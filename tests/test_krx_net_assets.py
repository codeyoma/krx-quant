"""The single-stock ETFs' net assets from the KRX Open API (#305, ``krx_net_assets``), fetched through
the fake KRX service (``tests/fake_krx_openapi.py``).

Input: the service's day answers shaped as the server's probe saw them (``probe-krx-etf-305``,
2026-10-23): ``OutBlock_1``, every value a string without thousands commas, the products' codes in
``ISU_CD`` as they are, ``BAS_DD`` the day asked, rows from the listing day (2026-05-27) on, two
other ETFs beside them. One product is missing from one day's answer and one has no net assets
(``-``) on another. Every number is made up and distinct (a sentinel). Output: the rows of the
state file, the days asked and when, the log lines (counts, dates, times and codes, never a value),
and which source a day's demand reads.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant import loki
from krx_quant.feeds.base import KST
from krx_quant.krx_net_assets import (
    MAX_FAILED,
    KrxClient,
    KrxNetAssetKeeper,
    keep_krx_net_assets,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.net_assets import KIS, KRX, UNIT, net_assets_for, read_net_assets, write_net_assets
from krx_quant.products import PRODUCTS, Product
from tests.fake_krx_openapi import KEY, PATH, FakeKrx, fake_krx

HANGUL_DAY = date(2026, 10, 9)
CALENDAR = MarketCalendar(
    {
        d: d.weekday() < 5 and d != HANGUL_DAY
        for d in (date(2026, 5, 1) + timedelta(days=i) for i in range(240))
    }
)
LISTED = date(2026, 5, 27)
THURSDAY, FRIDAY = date(2026, 10, 22), date(2026, 10, 23)
MONDAY, TUESDAY = date(2026, 10, 26), date(2026, 10, 27)
ABSENT, ABSENT_ON = "0197X0", date(2026, 8, 14)  # missing from that day's answer
NO_FIGURE, NO_FIGURE_ON = "0194N0", date(2026, 9, 1)  # its net assets "-" that day
CODES = [p.code for p in PRODUCTS]


def at(hhmm: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def total(code: str, day: date) -> int:
    """A made-up net asset total in won, distinct by product and day."""
    return 7_000_000_000 + 1_000_003 * CODES.index(code) + 9_973 * day.toordinal() % 1_000_000_007


def shares(code: str, day: date) -> int:
    return 3_170_011 + 101 * CODES.index(code) + day.toordinal() % 89


def nav(code: str, day: date) -> str:
    return f"{12_000 + 17 * CODES.index(code) + day.toordinal() % 997 + 0.37:.2f}"


def product_row(code: str, day: date) -> dict[str, str]:
    return {
        "BAS_DD": f"{day:%Y%m%d}",
        "ISU_CD": code,
        "ISU_NM": f"테스트 상품 {code}",
        "TDD_CLSPRC": "12345",
        "NAV": nav(code, day),
        "MKTCAP": str(12_345 * shares(code, day)),
        "INVSTASST_NETASST_TOTAMT": "-"
        if (code, day) == (NO_FIGURE, NO_FIGURE_ON)
        else str(total(code, day)),
        "LIST_SHRS": str(shares(code, day)),
    }


def answer(day: date) -> list[dict[str, Any]]:
    """KRX's answer of a day: two other ETFs, and the products from their listing."""
    others = [{**product_row(CODES[0], day), "ISU_CD": c} for c in ("069500", "133690")]
    if day < LISTED:
        return others
    return others + [product_row(c, day) for c in CODES if (c, day) != (ABSENT, ABSENT_ON)]


def sentinels(days: list[date]) -> set[str]:
    """Every value the fake gives for the products, as text."""
    found = set()
    for day in days:
        for code in CODES:
            found |= {str(total(code, day)), str(shares(code, day)), nav(code, day)}
    return found


def asked_days(fake: FakeKrx) -> list[date]:
    return [datetime.strptime(q["basDd"], "%Y%m%d").date() for _, q, _ in fake.requests]


@asynccontextmanager
async def keeper_for(
    rows: Callable[[date], list[dict[str, Any]] | int],
    state: Path,
    clock: Callable[[], datetime],
    key: str = KEY,
    products: tuple[Product, ...] = PRODUCTS,
    after: Any = None,
) -> AsyncIterator[tuple[KrxNetAssetKeeper, FakeKrx]]:
    async with fake_krx(rows) as fake, aiohttp.ClientSession() as session:

        async def no_wait(seconds: float) -> None:
            return None

        keeper = KrxNetAssetKeeper(
            KrxClient(session, fake.url, key), CALENDAR, state, products, clock, sleep=no_wait, after=after
        )
        yield keeper, fake


def logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


async def test_the_first_run_fetches_every_trading_day_from_the_listing_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On Sunday 06:00 (the deploy): every trading day from the listing whose rows are out, so to
    Thursday (Friday's are out on Monday 08:00), one call a day, the key in the header alone. A
    second run asks nothing: every day is held."""
    state = tmp_path / "state"
    now = at("06:00", date(2026, 10, 25))
    computed: list[datetime] = []

    async def after(when: datetime) -> None:
        computed.append(when)

    caplog.set_level(logging.DEBUG, logger="krx_quant")
    async with keeper_for(answer, state, lambda: now, after=after) as (keeper, fake):
        changed = await keeper.run()
        again = await keeper.run()

    days = CALENDAR.trading_days(LISTED, THURSDAY)
    assert asked_days(fake) == days, "oldest first, no holiday, nothing before the listing, not Friday"
    assert all(
        path == PATH and query == {"basDd": f"{d:%Y%m%d}"}
        for (path, query, _), d in zip(fake.requests, days, strict=True)
    )
    assert {header for _, _, header in fake.requests} == {KEY}
    assert (again, computed) == (0, [now]), "the days computed again once, after the rows came"
    rows = {(r["code"], r["date"]): r for r in read_net_assets(state)}
    assert changed == len(rows) == len(days) * len(CODES) - 2
    assert (ABSENT, ABSENT_ON) not in rows and (NO_FIGURE, NO_FIGURE_ON) not in rows
    assert rows[("0193W0", THURSDAY)] == {
        "code": "0193W0",
        "date": THURSDAY,
        "source": KRX,
        "net_assets": total("0193W0", THURSDAY),  # won, as KRX gives it
        "fetched_at": now,
        "shares": shares("0193W0", THURSDAY),
        "nav": float(nav("0193W0", THURSDAY)),
    }
    text = logged(caplog)
    assert f"KRX net assets: {len(days)} days asked (2026-05-27 – 2026-10-22), {len(days)} in" in text
    for value in sentinels(days):
        assert value not in text, value
    assert KEY not in text


async def test_kis_stands_in_for_a_product_or_day_krx_lacks(tmp_path: Path) -> None:
    """Source precedence (``net_assets.SOURCES``): KRX's row first, product by product; KIS's
    where KRX has none."""
    state = tmp_path / "state"
    kis = [
        {"code": c, "date": d, "source": KIS, "net_assets": 5 * UNIT, "fetched_at": at("06:12", d)}
        for c in CODES
        for d in (ABSENT_ON, NO_FIGURE_ON)
    ]
    write_net_assets(state, kis)
    async with keeper_for(answer, state, lambda: at("08:05", date(2026, 9, 2))) as (keeper, _):
        await keeper.run([ABSENT_ON, NO_FIGURE_ON])

    rows = read_net_assets(state)
    held = net_assets_for(rows, CALENDAR, date(2026, 8, 17))  # the Monday after ABSENT_ON
    assert {c: r["source"] for c, r in held.items()} == {c: KIS if c == ABSENT else KRX for c in CODES}
    held = net_assets_for(rows, CALENDAR, date(2026, 9, 2))
    assert held[NO_FIGURE]["source"] == KIS and held["0193W0"]["net_assets"] == total("0193W0", NO_FIGURE_ON)


class _Stop(Exception):
    pass


def held_to_thursday(state: Path, products: tuple[Product, ...]) -> None:
    """KRX's rows of every trading day to Thursday, and KIS's of Friday."""
    rows = [
        {
            "code": p.code,
            "date": d,
            "source": KRX,
            "net_assets": total(p.code, d),
            "fetched_at": at("06:00", d),
        }
        for p in products
        for d in CALENDAR.trading_days(LISTED, THURSDAY)
    ]
    rows += [
        {"code": p.code, "date": FRIDAY, "source": KIS, "net_assets": 70 * UNIT, "fetched_at": at("06:12")}
        for p in products
    ]
    write_net_assets(state, rows)


async def test_each_trading_morning_from_0805_the_day_before_every_five_minutes_until_it_is_in(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Started Monday 07:20, before Friday's rows are out (08:00): nothing asked then. From 08:05
    Friday, every five minutes until KRX has it (08:12): in at 08:15. Then Tuesday 08:05."""
    state = tmp_path / "state"
    products = tuple(p for p in PRODUCTS if p.code in ("0193W0", "0197X0"))
    held_to_thursday(state, products)
    clock = [at("07:20")]
    woke: list[datetime] = []

    def rows(day: date) -> list[dict[str, Any]]:
        return answer(day) if clock[0] >= at("08:12") else []

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        woke.append(clock[0])
        if clock[0] >= at("08:05", TUESDAY):
            raise _Stop

    caplog.set_level(logging.INFO, logger="krx_quant")
    async with keeper_for(rows, state, lambda: clock[0], products=products) as (keeper, fake):
        with pytest.raises(_Stop):
            await keep_krx_net_assets(keeper, lambda: clock[0], sleep)

    assert woke == [at("08:05"), at("08:10"), at("08:15"), at("08:05", TUESDAY)]
    assert asked_days(fake) == [FRIDAY] * 3
    friday = {r["code"]: r for r in read_net_assets(state) if r["date"] == FRIDAY and r["source"] == KRX}
    assert {r["fetched_at"] for r in friday.values()} == {at("08:15")}
    held = net_assets_for(read_net_assets(state), CALENDAR, MONDAY)
    assert {r["source"] for r in held.values()} == {KRX}, "Monday's demand reads KRX's from then on"
    assert "KRX net assets of 2026-10-23 in at 08:15:00: 2 products" in logged(caplog)


async def test_not_in_by_the_closing_auction_the_demand_reads_kis_s(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Friday's rows never come on Monday: asked every five minutes from 08:05 to 15:15, then a
    warning, and Monday's demand reads KIS's net assets of Friday; asked again Tuesday 08:05 with
    every day lacking."""
    state = tmp_path / "state"
    products = tuple(p for p in PRODUCTS if p.code == "0193W0")
    held_to_thursday(state, products)
    clock = [at("07:20")]
    woke: list[datetime] = []
    demand_reads: list[str] = []

    async def sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)
        woke.append(clock[0])
        if clock[0] >= at("15:20") and not demand_reads:
            held = net_assets_for(read_net_assets(state), CALENDAR, MONDAY)
            demand_reads.extend(r["source"] for r in held.values())
        if clock[0] >= at("08:05", TUESDAY):
            raise _Stop

    caplog.set_level(logging.INFO, logger="krx_quant")
    async with keeper_for(lambda d: [], state, lambda: clock[0], products=products) as (keeper, fake):
        with pytest.raises(_Stop):
            await keep_krx_net_assets(keeper, lambda: clock[0], sleep)

    retries = [at("08:05") + timedelta(minutes=5 * i) for i in range(87)]
    assert retries[-1] == at("15:15")
    assert woke == [*retries, at("08:05", TUESDAY)]
    assert asked_days(fake) == [FRIDAY] * 87
    assert demand_reads == [KIS], "the 15:20 demand reads KIS's"
    assert (
        "KRX net assets of 2026-10-23 not in by the closing auction: the day's demand reads KIS's"
        in logged(caplog)
    )


async def test_krx_s_day_is_set_against_kis_s_and_the_lines_carry_no_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """KIS's net assets in 억원 against KRX's in won, the same day: equal within its rounding (half an
    억, even over 0.5% on a small product) or 0.5%; one product 1% off is flagged, by its code."""
    state = tmp_path / "state"
    products = tuple(p for p in PRODUCTS if p.code in ("0193W0", "0195R0", "0194M0", "0192M0"))
    small = "0192M0"

    def rows(day: date) -> list[dict[str, Any]]:
        out = answer(day)
        for row in out:
            if row["ISU_CD"] == small:
                row["INVSTASST_NETASST_TOTAMT"] = "3040000000"  # 30.4억: KIS's 30억 is within half an 억
        return out

    kis = {"0193W0": total("0193W0", THURSDAY), "0195R0": round(total("0195R0", THURSDAY) * 1.004)}
    kis["0194M0"] = round(total("0194M0", THURSDAY) * 1.01)
    kis[small] = 30 * UNIT
    write_net_assets(
        state,
        [
            {"code": c, "date": THURSDAY, "source": KIS, "net_assets": v, "fetched_at": at("06:12", FRIDAY)}
            for c, v in kis.items()
        ],
    )
    caplog.set_level(logging.DEBUG, logger="krx_quant")
    async with keeper_for(rows, state, lambda: at("08:05", FRIDAY), products=products) as (keeper, _):
        await keeper.run([THURSDAY])

    text = logged(caplog)
    assert "KRX net assets of 2026-10-22 against KIS's: 4 compared, 1 over 0.5% (0194M0)" in text
    assert [r.levelname for r in caplog.records if "against KIS's" in r.getMessage()] == ["WARNING"]
    for value in {*sentinels([THURSDAY]), *map(str, kis.values()), "3040000000"}:
        assert value not in text, value


async def test_a_refused_key_stops_the_run_and_never_shows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """401 (the key without the service approved): logged with KRX's FAQ reading, the run stops after
    ``MAX_FAILED`` days in a row; nothing kept, the key nowhere in the lines."""
    state = tmp_path / "state"
    wrong = "a-key-krx-refuses-0123456789"
    caplog.set_level(logging.DEBUG, logger="krx_quant")
    async with keeper_for(answer, state, lambda: at("06:00"), key=wrong) as (keeper, fake):
        assert await keeper.run() == 0

    assert len(fake.requests) == MAX_FAILED
    text = logged(caplog)
    assert "KRX net assets of 2026-05-27 not fetched: HTTP 401 (KRX FAQ: 인증키만 받고" in text
    assert f"KRX net assets: stopped after {MAX_FAILED} failures in a row" in text
    assert wrong not in text and read_net_assets(state) == []


async def test_a_failure_s_traceback_reaches_loki_as_its_template_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that fails (here the days' computation after the rows came, its message carrying a
    value) is logged with its traceback on the server, and to Loki as the line's template alone."""
    state = tmp_path / "state"
    clock = [at("06:00", date(2026, 10, 25))]

    async def after(when: datetime) -> None:
        raise ValueError(f"a message carrying {total('0193W0', THURSDAY)}")

    async def sleep(seconds: float) -> None:
        raise _Stop

    caplog.set_level(logging.INFO, logger="krx_quant")
    async with keeper_for(answer, state, lambda: clock[0], after=after) as (keeper, _):
        with pytest.raises(_Stop):
            await keep_krx_net_assets(keeper, lambda: clock[0], sleep)

    [failed] = [r for r in caplog.records if r.getMessage() == "the KRX net assets failed"]
    assert failed.exc_info is not None and getattr(failed, loki.VALUES) is False
