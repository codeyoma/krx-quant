"""The single-stock ETFs' net assets kept for the closing flow (#305, ``net_assets``), fetched
through the fake KIS.

Input: KIS's ETF/ETN current price and daily NAV of the 16 products, shaped as the server's probe
saw them (``probe-nav-305``, 2026-10-21): the current price's ``prdy_last_nav`` is the NAV of the
trading day before KIS's day, its ``etf_ntas_ttam`` the net assets of that day in 억원; the daily
NAV newest first, with a placeholder row of the fetch's day before its session. Every number is
made up. Output: the rows of the state file, and when the task asks again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nav_probe import DAILY, PRICE
from krx_quant.net_assets import (
    KIS,
    UNIT,
    NetAssetKeeper,
    keep_net_assets,
    net_assets_for,
    read_net_assets,
)
from krx_quant.products import PRODUCTS, Product
from krx_quant.ratelimit import RateLimiter
from tests.kis_rest import requests_for, rest_client

HANGUL_DAY = date(2026, 10, 9)
CALENDAR = MarketCalendar(
    {
        d: d.weekday() < 5 and d != HANGUL_DAY
        for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(120))
    }
)
THURSDAY = date(2026, 10, 22)  # the demand's day
WEDNESDAY, TUESDAY = date(2026, 10, 21), date(2026, 10, 20)
CODES = [p.code for p in PRODUCTS]
NO_FIGURE = "0194N0"  # its total is 0: no figure
EMPTY = "0198B0"  # its total is empty


def at(hhmm: str, day: date = THURSDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def nav(code: str, day: date) -> str:
    """A made-up NAV of a product on a day: never the same on two days."""
    return f"{10_000 + 37 * CODES.index(code) + day.toordinal() % 1000 + 0.25:.2f}"


def total(code: str, day: date) -> int:
    """A made-up net asset total in 억원, of the day its NAV is of."""
    return 1_000 + 10 * CODES.index(code) + day.day


def ok(**outputs: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", **outputs}


@dataclass
class KisNav:
    """KIS's two queries on its day ``day``: the current price's previous NAV and total are of the
    trading day before it; the daily NAV runs to ``day``, whose row, on its own date (before its
    session), is a placeholder carrying the previous NAV, as KIS's might."""

    day: date
    totals: dict[str, str] = field(default_factory=dict)  # a product's total in place of its own
    navs: dict[tuple[str, date], str] = field(default_factory=dict)  # a day's NAV in place of its own

    def nav_of(self, code: str, day: date) -> str:
        return self.navs.get((code, day), nav(code, day))

    def price(self, params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_COND_MRKT_DIV_CODE"] == "J"
        code = params["FID_INPUT_ISCD"]
        before = CALENDAR.previous_trading_day(self.day)
        return ok(
            output={
                "etf_ntas_ttam": self.totals.get(code, str(total(code, before))),
                "prdy_last_nav": self.nav_of(code, before),
                "nav": self.nav_of(code, before),  # before its session: the last one
                "lstn_stcn": "3170000",
                "etf_trc_ert_mltp": "2.00",
            }
        )

    def daily(self, params: dict[str, str]) -> dict[str, Any]:
        code = params["FID_INPUT_ISCD"]
        first = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
        last = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
        before = CALENDAR.previous_trading_day(self.day)
        days = CALENDAR.trading_days(first, min(last, self.day))
        rows = [
            {
                "stck_bsop_date": d.strftime("%Y%m%d"),
                # The day's placeholder on its own date: the previous NAV again.
                "nav": self.nav_of(code, before) if d == self.day == last else self.nav_of(code, d),
                "stck_clpr": "10000",
            }
            for d in reversed(days)
        ]
        return ok(output=rows)

    def serve(self, kis: FakeKis) -> None:
        kis.rest_responses[PRICE.tr] = self.price
        kis.rest_responses[DAILY.tr] = self.daily


def keeper_of(client: Any, tmp_path: Path, products: tuple[Product, ...] = PRODUCTS) -> NetAssetKeeper:
    return NetAssetKeeper(client, CALENDAR, tmp_path / "state", products, RateLimiter(1, 0.001, margin=0))


async def keep(kis: FakeKis, tmp_path: Path, now: datetime) -> int:
    async with rest_client(kis, tmp_path, now) as client:
        return await keeper_of(client, tmp_path).run(now)


def stored(tmp_path: Path) -> dict[tuple[str, date], dict[str, Any]]:
    return {(r["code"], r["date"]): r for r in read_net_assets(tmp_path / "state")}


async def test_a_morning_fetch_keeps_each_product_s_net_assets_of_the_trading_day_before(
    kis: FakeKis, tmp_path: Path
) -> None:
    KisNav(THURSDAY, totals={NO_FIGURE: "0", EMPTY: ""}).serve(kis)

    changed = await keep(kis, tmp_path, at("07:20"))

    kept = stored(tmp_path)
    assert changed == len(PRODUCTS) - 2
    assert set(kept) == {(c, WEDNESDAY) for c in CODES if c not in (NO_FIGURE, EMPTY)}, (
        "of Wednesday, the day before the fetch's, not of its placeholder row; none without a figure"
    )
    row = kept[("0193W0", WEDNESDAY)]
    assert row == {
        "code": "0193W0",
        "date": WEDNESDAY,
        "source": KIS,
        "net_assets": total("0193W0", WEDNESDAY) * UNIT,
        "fetched_at": at("07:20"),
    }
    held = net_assets_for(read_net_assets(tmp_path / "state"), CALENDAR, THURSDAY)
    assert set(held) == set(CODES) - {NO_FIGURE, EMPTY}, "Thursday's demand reads Wednesday's"
    daily = requests_for(kis, DAILY.tr)[0]
    assert (daily["FID_INPUT_DATE_1"], daily["FID_INPUT_DATE_2"]) == ("20261008", "20261022")


async def test_before_kis_moves_to_its_new_day_the_rows_are_of_the_day_before_that(
    kis: FakeKis, tmp_path: Path
) -> None:
    KisNav(WEDNESDAY).serve(kis)  # at 06:00 KIS's answer is still Wednesday's, as at 23:50

    await keep(kis, tmp_path, at("06:00"))

    assert {d for _, d in stored(tmp_path)} == {TUESDAY}
    assert net_assets_for(read_net_assets(tmp_path / "state"), CALENDAR, THURSDAY) == {}


async def test_fetching_again_keeps_the_file_and_a_new_value_replaces_its_row(
    kis: FakeKis, tmp_path: Path
) -> None:
    answers = KisNav(THURSDAY)
    answers.serve(kis)
    await keep(kis, tmp_path, at("07:20"))
    first = (tmp_path / "state" / "closing_flow" / "net_assets.parquet").read_bytes()

    again = await keep(kis, tmp_path, at("20:30"))

    assert again == 0
    assert (tmp_path / "state" / "closing_flow" / "net_assets.parquet").read_bytes() == first
    answers.totals["0193W0"] = "1500"  # KIS gives another figure for the same day
    assert await keep(kis, tmp_path, at("20:40")) == 1
    row = stored(tmp_path)[("0193W0", WEDNESDAY)]
    assert (row["net_assets"], row["fetched_at"]) == (1500 * UNIT, at("20:40"))
    assert stored(tmp_path)[("0195R0", WEDNESDAY)]["fetched_at"] == at("07:20"), "the others as they were"


async def test_a_previous_nav_of_no_day_or_of_two_days_is_not_kept(kis: FakeKis, tmp_path: Path) -> None:
    answers = KisNav(THURSDAY)
    answers.navs[("0193W0", WEDNESDAY)] = "1.00"  # the daily NAV of Wednesday differs from…
    answers.navs[("0195R0", TUESDAY)] = nav("0195R0", WEDNESDAY)  # …two days with the same NAV
    original = answers.price

    def price(params: dict[str, str]) -> dict[str, Any]:
        answer = original(params)
        if params["FID_INPUT_ISCD"] == "0193W0":
            answer["output"]["prdy_last_nav"] = nav("0193W0", WEDNESDAY)  # …the current price's
        return answer

    answers.serve(kis)
    kis.rest_responses[PRICE.tr] = price

    await keep(kis, tmp_path, at("07:20"))

    codes = {c for c, _ in stored(tmp_path)}
    assert "0193W0" not in codes and "0195R0" not in codes
    assert len(codes) == len(PRODUCTS) - 2


class _Stop(Exception):
    pass


async def test_the_task_asks_the_lacking_products_every_ten_minutes_until_the_closing_auction(
    kis: FakeKis, tmp_path: Path
) -> None:
    """Started at 07:20, before KIS moves to Thursday (07:45): the retries at 07:30 and 07:40 find
    Tuesday's again; at 07:50 Wednesday's are in, but for the product without a figure, asked every
    ten minutes until the closing auction (15:20), then every product at 20:30. Three products:
    fewer calls."""
    products = tuple(p for p in PRODUCTS if p.code in ("0193W0", "0195R0", NO_FIGURE))
    answers = KisNav(WEDNESDAY, totals={NO_FIGURE: "0"})
    answers.serve(kis)
    clock = [at("07:20")]
    woke: list[datetime] = []

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        woke.append(clock[0])
        if clock[0] >= at("07:45"):
            answers.day = THURSDAY
        if clock[0] >= at("20:30"):
            raise _Stop  # the evening's run is due: done

    async with rest_client(kis, tmp_path, clock[0]) as client:
        with pytest.raises(_Stop):
            await keep_net_assets(keeper_of(client, tmp_path, products), lambda: clock[0], sleep)

    retries = [at("07:30") + timedelta(minutes=10 * i) for i in range(47)]  # to 15:10
    assert woke == [*retries, at("20:30")]
    kept = stored(tmp_path)
    assert kept[("0193W0", WEDNESDAY)]["fetched_at"] == at("07:50"), "the first fetch after KIS moved"
    assert (NO_FIGURE, WEDNESDAY) not in kept
    asked = [q["FID_INPUT_ISCD"] for q in requests_for(kis, PRICE.tr)]
    codes = [p.code for p in products]
    assert asked[:3] == codes, "every product at the start"
    assert asked[3:12] == codes * 3, "the lacking ones until 07:50: all three"
    assert asked[12:] == [NO_FIGURE] * (len(retries) - 3), "then the one without a figure only"
