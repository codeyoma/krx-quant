"""The mock practice's spacing (#598, ``paper.spaced``): a practice rise buys only at a bar close at
least ``Rules.practice_spacing`` (15 minutes) after the bar close of the day's last practice buy order,
else it is skipped ``spacing``; the champion's cells are never spaced. So the practice's buys, which
on 2026-11-04 and 11-05 were gone by 13:14 and 11:04, reach the afternoon and the time exits before
the closing auction.

KIS is the fake one (``tests/test_trader_mock.py``) and the practice cell the 4-minute barrier's
breiman at θ 0.6, kept for the review cycle (``tests/test_trader_practice.py``). Its rises (up_first
0.7) are read 2.3 seconds after their bar close; the book is 70,000 / 70,100 unless said, so that KIS
fills a share at 70,050 and a time exit sells it at 70,050 too, a net loss of the costs. Every value is
made up.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.config import SettingsError
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.trader import MOCK, SYMBOL, report
from krx_quant.trader.limits import RUNNING
from krx_quant.trader.paper import (
    BUY_LIMIT,
    PAST_LAST_EXIT,
    SPACING,
    TAKE_PROFIT,
    TIME,
    Rules,
    spaced,
)
from krx_quant.trader.selection import Pick
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME
from tests.kis_doubles import Fill, kis_mock, rows
from tests.test_trader_practice import frozen_here, practised
from tests.test_trader_shadow import BOUGHT, DAY, TAKEN, TEN, MockDesk
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    Barrier,
    Day,
    Read,
    at,
    pick,
    quote,
    whole_rules,
)

__all__ = ["kis_mock"]  # the fixture, used here


def rise(hm: str) -> list[Any]:
    """The practice cell's rise of the bar close ``HH:MM`` (of another day with its date before it),
    read 2.3 seconds after it, and the quote after it (the one a buy decided on it fills on)."""
    return [Read(f"{hm}:02.3", [Barrier(f"{hm}:00")]), quote(f"{hm}:02.5", 70_000, 70_100)]


def practice_pick() -> Pick:
    """The practice cell as a pick: a share at a time, at its frozen θ (#427, #474)."""
    return replace(pick(BARRIER, quantity=1), threshold=0.6, practice=True)


def reasons(state: Path) -> dict[str, str | None]:
    """The mock book's decision of each bar close (``HH:MM``): ``buy`` or why it skipped."""
    return {
        d["bar_close"][11:16]: "buy" if d["action"] == "buy" else d["reason"]
        for d in rows(state, "decisions")
        if d["mode"] == MOCK
    }


def bought(trades: list[dict[str, Any]]) -> list[str]:
    """The bar closes (``HH:MM``) of the practice's buys, oldest first."""
    return sorted(t["bar_close"][11:16] for t in trades if t["practice"])


# The setting


def test_the_spacing_is_15_minutes_unless_set_and_0_spaces_none() -> None:
    """Not a risk rule (#471 lets ``.env`` only tighten those): the practice's pace, set either way."""
    default = TraderSettings.from_env({}, None)
    assert default.practice_spacing_minutes == 15.0
    assert Rules.of(default, FEE).practice_spacing == timedelta(minutes=15)
    for value, minutes in (("25", 25.0), ("5", 5.0), ("0", 0.0)):
        found = TraderSettings.from_env({"KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES": value}, None)
        assert Rules.of(found, FEE).practice_spacing == timedelta(minutes=minutes)
    for wrong in ("-1", "inf", "soon"):
        with pytest.raises(SettingsError, match="KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES"):
            TraderSettings.from_env({"KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES": wrong}, None)


def test_a_practice_rise_is_spaced_from_the_bar_close_of_the_day_s_last_practice_buy() -> None:
    rules = whole_rules()
    last = at("10:00:00")
    assert spaced(rules, at("10:00:00"), None) is None, "the day's first"
    assert spaced(rules, at("10:14:00"), last) == SPACING
    assert spaced(rules, at("10:15:00"), last) is None, "15 minutes on: it may buy"
    assert spaced(whole_rules(practice_spacing=timedelta(0)), at("10:01:00"), last) is None


# The practice spaced


async def test_a_practice_buy_sooner_than_the_spacing_is_skipped_and_kept_as_spacing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The 10:00 rise buys a share on KIS and takes profit at 10:02:30. The rises of 10:08 and 10:14 are
    less than 15 minutes after it: skipped ``spacing``, nothing sent to KIS. The 10:15 rise, 15
    minutes on, buys. The nightly report counts the skips."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    desk = MockDesk(tmp_path, fake, account, selection=practised(), practice=True)

    await desk.run([TEN, BOUGHT, TAKEN, Fill("10:02:30"), *rise("10:08"), *rise("10:14"), *rise("10:15")])

    assert reasons(tmp_path) == {"10:00": "buy", "10:08": SPACING, "10:14": SPACING, "10:15": "buy"}
    first, second = sorted(desk.trades(), key=lambda t: t["bar_close"])
    assert first["practice"] and first["exit"]["reason"] == TAKE_PROFIT
    assert second["practice"] and second["entry"]["quantity"] == 1 and second["exit"] is None
    assert len(rows(tmp_path, "unfilled_buys")) == 2, "two buys sent to KIS, the skips none"
    assert desk.state_now is not None and desk.state_now["risk"]["buys"] == 2
    assert desk.status == RUNNING
    text = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR))
    assert "읽은 예측(연습 칸 고르기의 챔피언 장부): 읽은 예측 4 · 산 것 2 · 사지 않은 것: spacing 2" in (
        text.splitlines()
    )


async def test_a_practice_buy_given_up_before_its_order_spaces_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The first quote after the 10:00 rise asks the target already (``target_reached``): no order goes
    to KIS, so the 10:01 rise buys."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    desk = MockDesk(tmp_path, fake, account, selection=practised(), practice=True)

    await desk.run([TEN, quote("10:00:02.5", 70_200, 70_300), *rise("10:01")])

    assert reasons(tmp_path) == {"10:00": "buy", "10:01": "buy"}
    [unfilled] = [d for d in rows(tmp_path, "decisions") if d["reason"] is not None]
    assert unfilled["reason"] == "target_reached"
    [trade] = desk.trades()
    assert trade["bar_close"] == "2026-10-14T10:01:00+09:00"


async def test_a_restart_spaces_from_the_practice_buy_kept_before_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The spacing counts from the store (the buy orders kept before they are sent, #406), so a trader
    started again at 10:05 skips the 10:08 rise as the one before would have."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    before = MockDesk(tmp_path, fake, account, selection=practised(), practice=True)
    await before.run([TEN, BOUGHT, TAKEN, Fill("10:02:30")])

    again = MockDesk(tmp_path, fake, account, selection=practised(), practice=True, started_at=at("10:05:00"))
    await again.run([*rise("10:08"), *rise("10:15")])

    assert reasons(tmp_path) == {"10:00": "buy", "10:08": SPACING, "10:15": "buy"}


def test_a_paper_book_spaces_its_practice_buys_and_the_spacing_ends_with_the_day(tmp_path: Path) -> None:
    """The same rule in a paper book, from its practice trades: the 15:00 buy sells at its 15:04 time
    barrier, and spaces nothing of the next day, whose 09:05 rise buys and whose 09:10 rise is spaced."""
    day = Day(tmp_path, picks=[practice_pick()])

    trades = day.run(
        [
            *rise("15:00"),
            quote("15:03:59", 70_000, 70_100),
            quote("15:04:02", 70_000, 70_100),
            *rise("2026-10-15 09:05"),
            quote("2026-10-15 09:08:00", 70_300, 70_400),  # its take-profit
            *rise("2026-10-15 09:10"),
        ]
    )

    assert sorted(t["bar_close"] for t in trades) == [
        "2026-10-14T15:00:00+09:00",
        "2026-10-15T09:05:00+09:00",
    ]
    assert all(t["practice"] and t["entry"]["quantity"] == 1 for t in trades)
    [spaced_] = [d for d in day.decisions() if d["reason"] == SPACING]
    assert spaced_["bar_close"] == "2026-10-15T09:10:00+09:00"


def every_minute() -> list[Any]:
    """A day the practice cell rises at every bar close, 09:00 to 15:15 (``baseline-persist``'s worst
    case: its rise 1.00 whenever the last move was up), on a book of 70,000 / 70,100 after each read
    and 70,200 / 70,300 at each minute's end: a share bought at 70,050 is sold by its time barrier on
    the standing 70,200 bid, filled at 70,250, a net gain, so that no loss streak's cooldown holds."""
    events: list[Any] = []
    minute = at("09:00:00")
    while minute <= at("15:15:00"):
        hm = minute.strftime("%H:%M")
        events += [*rise(hm), quote(f"{hm}:59", 70_200, 70_300)]
        minute += timedelta(minutes=1)
    return events


def every(minutes: int) -> list[str]:
    """20 bar closes (``HH:MM``) from 09:05, ``minutes`` apart."""
    return [(at("09:05:00") + timedelta(minutes=minutes * n)).strftime("%H:%M") for n in range(20)]


@pytest.mark.parametrize(("spacing", "buys"), [(15, every(15)), (0, every(5))])
async def test_spaced_a_day_rising_every_minute_practises_into_the_afternoon(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, spacing: int, buys: list[str]
) -> None:
    """On ``every_minute``'s day, spaced 15 minutes, the practice's 20 buys go at 09:05, 09:20, … 13:50,
    each sold by its time barrier on KIS, and the day's 20 buy orders are used (#533's 20 a day still
    fit). Without the spacing (0, as before #598) they go every five minutes, a trade's four and the
    minute its sale takes, and are gone by 10:40, as on 2026-11-05 by 11:04."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    rules = whole_rules(practice_spacing=timedelta(minutes=spacing))
    desk = MockDesk(tmp_path, fake, account, rules=rules, selection=practised(), practice=True)

    await desk.run(every_minute())

    trades = desk.trades()
    assert bought(trades) == buys
    assert {t["exit"]["reason"] for t in trades} == {TIME}
    assert all(t["net"] > 0 for t in trades), "no loss streak: the spacing alone spreads the buys"
    assert account.holdings[SYMBOL] == 0 and desk.status == RUNNING
    found = list(reasons(tmp_path).values())
    assert found.count("buy") == 20 and found.count(BUY_LIMIT) > 0
    # Between two buys: the four rises while its trade is open held (``holding``), ten spaced.
    assert found.count(SPACING) == (19 * 10 if spacing else 0)
    assert found.count(PAST_LAST_EXIT) == 4, "the bar closes of 15:12 to 15:15"
    assert desk.state_now is not None and desk.state_now["risk"]["buys"] == 20


async def test_the_practice_s_last_buy_of_the_day_sells_at_its_15_15_time_barrier_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The practice buys at 14:56; the 15:05 rise is spaced; the 15:11 rise, 15 minutes on and the 4-minute
    cell's last bar close, buys and is sold by its 15:15 time barrier on KIS, before the closing
    auction; the 15:12 rise would sell after it (``past_last_exit``). Nothing is left at the close."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    desk = MockDesk(tmp_path, fake, account, selection=practised(), practice=True)

    await desk.run(
        [
            *rise("14:56"),
            quote("14:59:59", 70_000, 70_100),
            quote("15:00:00.5", 70_000, 70_100),  # its time exit
            *rise("15:05"),
            *rise("15:11"),
            *rise("15:12"),
            quote("15:14:59", 70_000, 70_100),
            quote("15:15:00.5", 70_000, 70_100),
        ]
    )

    assert reasons(tmp_path) == {"14:56": "buy", "15:05": SPACING, "15:11": "buy", "15:12": PAST_LAST_EXIT}
    first, last = sorted(desk.trades(), key=lambda t: t["bar_close"])
    assert first["exit"]["reason"] == TIME and first["time_barrier"] == "2026-10-14T15:00:00+09:00"
    assert last["practice"] and last["exit"]["reason"] == TIME
    assert last["time_barrier"] == "2026-10-14T15:15:00+09:00"
    assert at("15:15:00") <= datetime.fromisoformat(last["exit"]["at"]) < at("15:20:00")
    assert [(o["side"], o["purpose"], o["status"]) for o in last["orders"]] == [
        ("buy", "entry", "filled"),
        ("sell", "take_profit", "cancelled"),
        ("sell", "time", "filled"),
    ]
    assert account.holdings[SYMBOL] == 0 and desk.status == RUNNING


def test_no_champion_cell_is_spaced(tmp_path: Path) -> None:
    """The spacing is the practice's alone: the champion's cell buys at 10:00 and again at 10:05."""
    day = Day(tmp_path, picks=[pick(BARRIER)])

    trades = day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            quote("10:02:00", 70_300, 70_400),  # its take-profit
            Read("10:05:02.3", [Barrier("10:05:00")]),
            quote("10:05:02.5", 70_000, 70_100),
        ]
    )

    assert sorted(t["bar_close"][11:16] for t in trades) == ["10:00", "10:05"]
    assert not any(t["practice"] for t in trades)
