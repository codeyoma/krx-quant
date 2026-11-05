"""Which feeds the collector registers, and in what order (fake KIS seam)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from tests.collector_doubles import (
    CALLS,
    KST,
    PUTS,
    STRIKES,
    collect,
    options_of,
    serve_masters,
    settings_for,
)

SYMBOLS = ("005930", "000660", "009150")


NOVEMBER_CALLS = [f"B01611C{i}" for i in range(len(STRIKES))]
NOVEMBER_PUTS = [f"C01611P{i}" for i in range(len(STRIKES))]


def full_settings(kis: FakeKis, tmp_path: Path, symbols: tuple[str, ...] = SYMBOLS) -> Settings:
    return replace(
        settings_for(kis, tmp_path, symbols=symbols),
        index_codes=("0001", "2001", "0013"),
        us_symbols=("NVDA", "MU", "AMD"),
        etf_symbols=("133690", "381180"),
        venue_trades=True,
    )


async def test_registers_every_feed_in_priority_order(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0UNCNT0", "009150")] = []

    await collect(kis, full_settings(kis, tmp_path))

    sent = [(s["body"]["input"]["tr_id"], s["body"]["input"]["tr_key"]) for s in kis.subscriptions]
    assert (
        sent
        == [
            # Market-wide context first, so it survives however many symbols are added.
            ("H0IFCNT0", "A01612"),  # KOSPI200 futures, nearest month from the master file
            ("H0CFCNT0", "A75610"),  # USD futures, nearest month from the master file
            ("H0MFCNT0", "A01612"),  # KOSPI200 night futures (#264), the night's month by the rules
            ("H0MFASP0", "A01612"),  # and their book
            ("HDFSCNT0", "RBAQNVDA"),  # US daytime session
            ("HDFSCNT0", "RBAQMU"),
            ("HDFSCNT0", "RBAQAMD"),
            *[(tr, sym) for sym in SYMBOLS for tr in ("H0UNCNT0", "H0UNASP0", "H0UNPGM0")],
            *[("H0UNMBC0", sym) for sym in SYMBOLS],
            ("H0UPCNT0", "0001"),
            ("H0UPCNT0", "2001"),
            ("H0UPCNT0", "0013"),
            *[("H0IOCNT0", code) for code in CALLS[1:6] + PUTS[1:6]],  # ATM +/- 2 strikes
            ("H0STCNT0", "133690"),  # KRX-listed US index ETFs trade on KRX only
            ("H0STCNT0", "381180"),
            *[("H0STCNT0", sym) for sym in SYMBOLS],
            *[("H0NXCNT0", sym) for sym in SYMBOLS],
            ("H0STASP0", "0193L0"),  # 005930's inverse ETF's KRX book (#606), last
        ]
    )
    assert len(sent) == 41, "KIS's session limit: the inverse ETF's book takes the last slot"


@pytest.mark.parametrize(
    ("day", "month"),
    [
        (8, "10"),  # October's expiry day itself: its options still trade until 15:20
        (12, "11"),  # October's expired on the 8th
    ],
)
async def test_options_are_taken_from_the_nearest_month_not_yet_expired(
    kis: FakeKis, tmp_path: Path, day: int, month: str
) -> None:
    # On 2026-10-12 the master KIS published still listed October's options, expired on the
    # 8th (the second Thursday), with an ATM strike (seen live).
    serve_masters(
        kis, [*options_of("202610", CALLS, PUTS), *options_of("202611", NOVEMBER_CALLS, NOVEMBER_PUTS)]
    )
    calls, puts = (CALLS, PUTS) if month == "10" else (NOVEMBER_CALLS, NOVEMBER_PUTS)
    kis.script[("H0IOCNT0", calls[3])] = []

    await collect(kis, settings_for(kis, tmp_path), now=datetime(2026, 10, day, 9, 30, tzinfo=KST))

    options = sorted(key for tr, key in kis.subscribed_keys() if tr == "H0IOCNT0")
    assert options == sorted(calls[1:6] + puts[1:6])


async def test_options_whose_last_day_came_early_for_a_holiday_are_passed_over(
    kis: FakeKis, tmp_path: Path
) -> None:
    # Were 2026-11-12, November's second Thursday, a holiday, November's options would last
    # trade on the 11th: a collector started on the 12th takes December's.
    state = tmp_path / "state"
    state.mkdir()
    closed = {"refreshed_on": "2026-11-12", "open_days": {"2026-11-12": False}}
    (state / "market_calendar.json").write_text(json.dumps(closed))
    december_calls = [f"B01612C{i}" for i in range(len(STRIKES))]
    december_puts = [f"C01612P{i}" for i in range(len(STRIKES))]
    serve_masters(
        kis,
        [
            *options_of("202611", NOVEMBER_CALLS, NOVEMBER_PUTS),
            *options_of("202612", december_calls, december_puts),
        ],
    )
    kis.script[("H0IOCNT0", december_calls[3])] = []

    await collect(kis, settings_for(kis, tmp_path), now=datetime(2026, 11, 12, 9, 30, tzinfo=KST))

    options = sorted(key for tr, key in kis.subscribed_keys() if tr == "H0IOCNT0")
    assert options == sorted(december_calls[1:6] + december_puts[1:6])


async def test_the_futures_are_recorded_even_when_no_option_month_can_be_chosen(
    kis: FakeKis, tmp_path: Path
) -> None:
    serve_masters(kis, options_of("202610", CALLS, PUTS))  # the only month listed has expired
    kis.script[("H0IFCNT0", "A01612")] = []

    await collect(kis, settings_for(kis, tmp_path), now=datetime(2026, 10, 12, 9, 30, tzinfo=KST))

    assert {("H0IFCNT0", "A01612"), ("H0CFCNT0", "A75610")} <= kis.subscribed_keys()
    assert not any(tr == "H0IOCNT0" for tr, _ in kis.subscribed_keys())


async def test_configured_futures_codes_are_used_when_the_masters_are_unavailable(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[("H0CFCNT0", "A75611")] = []
    settings = replace(settings_for(kis, tmp_path), kospi200_futures_code="A01703", usd_futures_code="A75611")

    await collect(kis, settings)

    assert {("H0IFCNT0", "A01703"), ("H0CFCNT0", "A75611")} <= kis.subscribed_keys()
    assert not any(tr == "H0IOCNT0" for tr, _ in kis.subscribed_keys()), "options need the master file"


async def test_an_added_symbol_gets_its_feeds_after_restart(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0NXCNT0", "035420")] = []

    await collect(kis, full_settings(kis, tmp_path, symbols=(*SYMBOLS, "035420")))

    registered = {key for tr, key in kis.subscribed_keys() if key == "035420"}
    feeds = {tr for tr, key in kis.subscribed_keys() if key == "035420"}
    assert registered == {"035420"}
    assert feeds == {"H0UNCNT0", "H0UNASP0", "H0UNPGM0", "H0UNMBC0", "H0STCNT0", "H0NXCNT0"}


async def test_the_inverse_etf_s_book_is_registered_only_with_the_traded_symbol(
    kis: FakeKis, tmp_path: Path
) -> None:
    # The replay it is for (#606) reads 005930's down calls: without 005930 there are none.
    kis.script[("H0UNCNT0", "000660")] = []

    await collect(kis, settings_for(kis, tmp_path, symbols=("000660",)))

    assert not any(tr == "H0STASP0" for tr, _ in kis.subscribed_keys())


async def test_a_cap_below_the_plan_cuts_the_inverse_etf_s_book_first(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[("H0NXCNT0", "009150")] = []

    await collect(kis, replace(full_settings(kis, tmp_path), ws_max_subscriptions=40))

    registered = kis.subscribed_keys()
    assert ("H0NXCNT0", "009150") in registered, "the feed before it keeps its slot"
    assert ("H0STASP0", "0193L0") not in registered
