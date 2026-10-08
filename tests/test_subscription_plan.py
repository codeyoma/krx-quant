"""Which feeds the collector registers, and in what order (fake KIS seam)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from tests.kis_frames import master_zips
from tests.test_collector import collect, settings_for

SYMBOLS = ("005930", "000660", "009150")


# Nearest expiry 202610 with ATM 1,072.5; two strikes each side are recorded, the rest are not.
STRIKES = ["1,065.0", "1,067.5", "1,070.0", "1,072.5", "1,075.0", "1,077.5", "1,080.0"]
CALLS = [f"B01610C{i}" for i in range(len(STRIKES))]
PUTS = [f"C01610P{i}" for i in range(len(STRIKES))]


def serve_masters(kis: FakeKis) -> None:
    options = [(c, "C", f"202610 {k}", k == "1,072.5") for c, k in zip(CALLS, STRIKES, strict=True)]
    options += [(p, "P", f"202610 {k}", k == "1,072.5") for p, k in zip(PUTS, STRIKES, strict=True)]
    options += [("B01611C3", "C", "202611 1,072.5", True)]  # a later expiry is ignored
    kis.masters = master_zips(
        kospi200_futures=[("A01703", "2"), ("A01612", "1")],
        usd_futures=[("A75611", "2"), ("A75610", "1")],
        options=options,
    )


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
            ("HDFSCNT0", "RBAQNVDA"),  # US daytime session
            ("HDFSCNT0", "RBAQMU"),
            ("HDFSCNT0", "RBAQAMD"),
            *[(tr, sym) for sym in SYMBOLS for tr in ("H0UNCNT0", "H0UNASP0", "H0UNPGM0")],
            *[("H0UNMBC0", sym) for sym in SYMBOLS],
            ("H0UPCNT0", "0001"),
            ("H0UPCNT0", "2001"),
            ("H0UPCNT0", "0013"),
            *[("H0IOCNT0", code) for code in CALLS[1:6] + PUTS[1:6]],  # ATM +/- 2 strikes
            ("H0UNCNT0", "133690"),  # KRX-listed US index ETFs
            ("H0UNCNT0", "381180"),
            *[("H0STCNT0", sym) for sym in SYMBOLS],
            *[("H0NXCNT0", sym) for sym in SYMBOLS],
        ]
    )


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
