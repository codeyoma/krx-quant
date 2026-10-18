"""KIS's daily supply joined by day to the day candles, as the collector's keepers write them
(#270): the structure check's turn of the month and Cox's flows (#264).

The structure check's first run (#270, 2026-10-18) found 2,452 stored days of each symbol's
foreign net buys, but only 395 of them, the same count for all three symbols, joined to a day
candle with a figure: about the trading days since NXT opened (2025-03-04). KIS's integrated
(``UN``) investor query answers 0 shares for each net buy on the days before, when there was no
integrated market. Here the fake KIS answers so, the keepers write the files (``CandleKeeper``,
``SupplyKeeper``: the dates as they store them), and the structure check and Cox read them: the
days join by date, and a 0 net buy is no figure (None), never a day of no net buying.

Every number is made up.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms.cox import FEATURES, Cox
from krx_quant.candles import CandleKeeper, read_candles, read_krx_days
from krx_quant.daily_features import DailyFiles, LiveFeatures, samples
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.macro import Observation
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE, SIGMA_DAYS
from krx_quant.ratelimit import RateLimiter
from krx_quant.structure_check import structure_check
from krx_quant.supply import INVESTORS, KIS, SupplyKeeper, read_supply
from krx_quant.us_daily import US_DAILY, write_us_daily
from tests.kis_rest import rest_client
from tests.test_candles import candles_api, kis_candles
from tests.test_supply import KisSupply, ok

CALENDAR = MarketCalendar()  # weekdays
SYMBOL = "005930"
FEE = 0.015 / 100
NXT_OPEN = date(2025, 3, 4)  # KIS's integrated investor figures begin
LAST = date(2025, 6, 30)  # a Monday
EVENING = datetime(2025, 6, 30, 21, 0, tzinfo=KST)  # the day over (after 20:30)


def trading_days(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if CALENDAR.is_trading_day(d)]


DAYS = trading_days(date(2024, 10, 1), LAST)


def net_buys(day: date) -> tuple[int, int, int]:
    """KIS's made-up (individual, institution, foreign) net buys of a day with a figure: the
    foreign one's sign alternates with the day."""
    k = day.toordinal()
    foreign = (1_000 + k % 500) * (1 if k % 2 else -1)
    return -foreign - 300, 300, foreign


def investors_api(days: list[date]) -> Callable[[dict[str, str]], dict[str, Any]]:
    """KIS's investor query (``FHPTJ04160001``, ``UN``): the 30 days to the day asked, newest
    first, each net buy "0" before NXT opened."""

    def respond(params: dict[str, str]) -> dict[str, Any]:
        assert params["FID_COND_MRKT_DIV_CODE"] == "UN"
        last = params["FID_INPUT_DATE_1"]
        rows = []
        for d in reversed(days):
            if d.strftime("%Y%m%d") > last:
                continue
            individual, institution, foreign = net_buys(d) if d >= NXT_OPEN else (0, 0, 0)
            rows.append(
                {
                    "stck_bsop_date": d.strftime("%Y%m%d"),
                    "prsn_ntby_qty": str(individual),
                    "orgn_ntby_qty": str(institution),
                    "frgn_ntby_qty": str(foreign),
                    "frgn_reg_ntby_qty": "0" if d < NXT_OPEN else str(foreign // 2),
                }
            )
            if len(rows) == 30:
                break
        return ok(output1={"stck_prpr": "1"}, output2=rows)

    return respond


async def keep_files(kis: FakeKis, tmp_path: Path) -> tuple[Path, Path]:
    """The day candles (integrated and KRX) and KIS's supply of ``DAYS``, through the keepers."""
    data, state = tmp_path / "data", tmp_path / "state"
    kis.rest_responses["FHKST03010100"] = candles_api(lambda market, symbol: kis_candles(DAYS))
    KisSupply(DAYS).serve(kis)
    kis.rest_responses[INVESTORS.tr] = investors_api(DAYS)
    pace = RateLimiter(1, 0.001, margin=0)
    async with rest_client(kis, tmp_path, EVENING) as client:
        candles = CandleKeeper(client, CALENDAR, data, (SYMBOL,), pace)
        assert await candles.update("day", SYMBOL, LAST)
        assert await candles.update_krx_days(SYMBOL, LAST)
        assert await SupplyKeeper(client, CALENDAR, state, (SYMBOL,), pace).run(EVENING) == 1
    return data, state


async def test_the_supply_days_join_the_candle_days_and_kis_s_zeros_are_no_figure(
    kis: FakeKis, tmp_path: Path
) -> None:
    data, state = await keep_files(kis, tmp_path)

    candles = read_candles(data, "day", SYMBOL)
    supply = read_supply(state, SYMBOL, KIS)
    # The keepers store the days alike: each supply day is a candle day, as the same type.
    assert [r["date"] for r in candles] == [r["date"] for r in supply] == DAYS
    assert {type(r["date"]) for r in [*candles, *supply, *read_krx_days(data, SYMBOL)]} == {date}
    # Before NXT opened, KIS's 0 shares are no figure; the other queries' values of those days stay.
    before = [r for r in supply if r["date"] < NXT_OPEN]
    assert before and all(r[f] is None for r in before for f in INVESTORS.columns)
    assert all(r["short_volume"] is not None for r in before)
    after = [r for r in supply if r["date"] >= NXT_OPEN]
    assert [(r["individual_net"], r["institution_net"], r["foreign_net"]) for r in after] == [
        net_buys(r["date"]) for r in after
    ]


async def test_the_turn_of_the_month_splits_every_day_with_a_foreign_figure(
    kis: FakeKis, tmp_path: Path
) -> None:
    data, state = await keep_files(kis, tmp_path)

    out = structure_check(data, state, CALENDAR, [SYMBOL], fee_rate=FEE)

    section = out.split("\n\n")[4]
    figures = [d for d in DAYS if d >= NXT_OPEN]
    assert f"{SYMBOL} 일봉 {DAYS[0]} – {LAST}, 외국인 순매수 {len(figures)}일({NXT_OPEN} – {LAST})" in section
    # Each day whose previous day has a foreign figure is in one of the two splits: all but the
    # last day's (no day after it here).
    split = 0
    for line in section.splitlines()[2:]:
        for part in line.split(" · ")[1:]:
            if part.startswith(("외국인 +", "외국인 −")):
                split += int(part.split()[2].removesuffix("일"))
    assert split == len(figures) - 1


async def test_cox_s_flows_are_missing_before_nxt_opened_not_zero_live_as_in_training(
    kis: FakeKis, tmp_path: Path
) -> None:
    data, state = await keep_files(kis, tmp_path)
    weekdays = [DAYS[0] - timedelta(days=10) + timedelta(days=i) for i in range(300)]
    for name in US_DAILY:
        write_us_daily(
            data,
            name,
            [Observation(d, 100.0 + i % 7) for i, d in enumerate(weekdays) if d.weekday() < 5 and d < LAST],
        )

    trained = samples(OPEN_TO_CLOSE, DailyFiles(data, state), [SYMBOL], FEATURES, FEE)
    found = {s.day: s.features for s in trained}

    volumes = {r["date"]: r["volume"] for r in read_candles(data, "day", SYMBOL)}
    assert sorted(found) == DAYS
    for i, day in enumerate(DAYS[1:], start=1):
        previous = DAYS[i - 1]
        features = found[day]
        if previous < NXT_OPEN:
            assert (features["foreign_net"], features["institution_net"]) == (None, None), day
        else:
            _, institution, foreign = net_buys(previous)
            assert features["foreign_net"] == pytest.approx(foreign / volumes[previous]), day
            assert features["institution_net"] == pytest.approx(institution / volumes[previous]), day
        # The other inputs join by day too.
        assert all(features[f] is not None for f in ("us_sox", "us_nvda", "us_mu", "vix")), day
        if i > SIGMA_DAYS:  # σ̂ scales them: 60 days before
            assert all(features[f] is not None for f in ("oc_1", "cc_1", "gap_1", "range_1")), day

    live = LiveFeatures(OPEN_TO_CLOSE, data, state, CALENDAR, [SYMBOL], FEE)
    files = DailyFiles(data, state)
    for day in (DAYS[DAYS.index(NXT_OPEN)], DAYS[DAYS.index(NXT_OPEN) + 1], LAST):
        history = [d for d in files.days(SYMBOL) if d.day < day]
        got = live(Cox(), SYMBOL, day, history, OPEN_TO_CLOSE.band(history))  # type: ignore[misc]
        assert got.as_dict() == pytest.approx(found[day].as_dict(), nan_ok=True), day
