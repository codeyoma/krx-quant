"""The trader's loss limits and pauses (#396, #393 "한도와 정지"): what stops it buying, what sells at
once, what lets it go on, on made-up quotes and predictions.

The example used throughout (``test_trader_paper.py``): the 10:00 barrier prediction buys 14 shares
at 70,100 (target 70,250, stop 69,850, time barrier 10:04). Its P&L sold at a bid b, after the
buy's commission (147.21) and the sale's commission (0.015%) and tax (0.20%), is
(b − 70,100) × 14 − 147.21 − b × 14 × 0.00215:

- at 70,050: −700 − 147.21 − 2,108.505 = −2,955.715;
- at 70,000: −1,400 − 147.21 − 2,107 = −3,654.21;
- at 69,900: −2,800 − 147.21 − 2,103.99 = −5,051.2 (above the stop);
- at 69,800 (the stop): −4,200 − 147.21 − 2,100.98 = −6,448.19.

The tests narrow the limits (``Rules``) so that a few ticks reach them, as ``.env`` may.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from krx_quant.trader import policy as policies
from krx_quant.trader.limits import CONTROL_SECONDS, ENTRIES_PAUSED, PAUSED, RUNNING, Guard, Limits
from krx_quant.trader.paper import Rules, shares
from krx_quant.trader.store import CUMULATIVE_LIMIT, DAILY_LIMIT, DB_NAME, REARM, Control, TraderStore
from tests.test_trader_marks import trade
from tests.trader_doubles import (
    FEE,
    AskPause,
    AskRearm,
    AskResume,
    Barrier,
    Day,
    Event,
    Read,
    Tick,
    at,
    quote,
)

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)


def rules(daily: float = 10.0, cumulative: float = 20.0) -> Rules:
    return Rules(fee_rate=FEE, daily_loss_percent=daily, cumulative_loss_percent=cumulative)


def on(day: str, hms: str) -> str:
    return f"{day} {hms}"


def next_day(day: str, at_: str = "10:00") -> list[Event]:
    """A barrier prediction of ``day`` at ``at_`` and the quote it buys on, if it may."""
    return [
        Read(on(day, f"{at_}:02.3"), [Barrier(on(day, f"{at_}:00"))]),
        quote(on(day, f"{at_}:02.5"), 70_000, 70_100),
    ]


def reasons(day: Day) -> list[tuple[str, str | None]]:
    return [(d["bar_close"][:16], d["reason"]) for d in day.decisions()]


def test_the_limits_in_won_are_their_percent_of_the_capital() -> None:
    assert (Limits(1_000_000, 10, 20).daily, Limits(1_000_000, 10, 20).cumulative) == (100_000, 200_000)
    assert Limits(500_000, 10, 20).daily == 50_000, "a narrower capital narrows them"


def test_the_open_position_alone_reaching_the_daily_limit_sells_it_and_stops_the_day(tmp_path: Path) -> None:
    """The daily limit at 0.5% (5,000 won). The bid at 69,900 puts the open position at −5,051.2,
    above its stop: the limit is reached on that quote and the position sold at its bid. Nothing
    more is bought that day; the next trading day buys again."""
    day = Day(tmp_path, rules=rules(daily=0.5))
    first, second = day.run(
        [
            TEN,
            BOUGHT,
            quote("10:01:00", 70_000, 70_100),  # −3,654.21: within the limit
            quote("10:01:30", 69_900, 70_000),  # −5,051.2: reached
            Read("10:05:02.3", [Barrier("10:05:00")]),
            quote("10:05:02.5", 70_000, 70_100),
            *next_day("2026-10-15"),
        ]
    )[::-1]

    assert first["exit"] == {"at": "2026-10-14T10:01:30+09:00", "price": 69_900, "reason": DAILY_LIMIT}
    assert first["net"] == pytest.approx(-5_051.2)
    assert [(o["purpose"], o["status"]) for o in first["orders"]] == [
        ("entry", "filled"),
        ("take_profit", "cancelled"),
        (DAILY_LIMIT, "filled"),
    ]
    assert reasons(day)[1] == ("2026-10-14T10:05", DAILY_LIMIT)
    assert second["entry"]["at"] == "2026-10-15T10:00:02.500000+09:00", "the next trading day buys"
    [notice] = day.notices
    assert "하루 손실 한도에 닿았습니다" in notice and "-5,051원" in notice and "다음 거래일" in notice
    [fourteenth, fifteenth] = day.days()
    assert (fourteenth["day"], fourteenth["status"], fourteenth["halt"]) == (
        "2026-10-14",
        DAILY_LIMIT,
        DAILY_LIMIT,
    )
    assert fourteenth["net"] == pytest.approx(-5_051.2) and fourteenth["trades"] == 1
    assert fourteenth["daily"] == {
        "limit": 5_000,
        "worst": pytest.approx(-5_051.2),
        "used": pytest.approx(1.01024),
    }
    assert (fifteenth["status"], fifteenth["halt"], fifteenth["trades"]) == (RUNNING, None, 0)


def test_a_stop_s_realized_loss_reaching_the_daily_limit_stops_the_day(tmp_path: Path) -> None:
    """The stop sells at 69,800 (−6,448.19): its own rule sells it, then the limit holds."""
    day = Day(tmp_path, rules=rules(daily=0.5))
    [trade] = day.run(
        [TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Read("10:05:02.3", [Barrier("10:05:00")])]
    )

    assert trade["exit"]["reason"] == "stop_loss"
    assert reasons(day)[1] == ("2026-10-14T10:05", DAILY_LIMIT)
    state = day.state_now("10:05:03")
    assert state["status"] == DAILY_LIMIT
    assert state["today"]["realized"] == pytest.approx(-6_448.19)
    assert state["limits"]["daily"]["reached_at"] == "2026-10-14T10:01:30+09:00"


def test_the_cumulative_limit_holds_across_days_until_it_is_re_armed(tmp_path: Path) -> None:
    """The daily limit at 1% (10,000), the cumulative at 0.7% (7,000). The 14th loses 3,654.21 at its
    time barrier. On the 15th the open position at 69,900 (−5,051.2) brings the cumulative to
    −8,705.41: reached, sold at once; the day's −5,051.2 is within its own limit. The 16th buys
    nothing until ``krx-quant trade rearm``, which counts again from then."""
    day = Day(tmp_path, rules=rules(daily=1.0, cumulative=0.7))
    trades = day.run(
        [
            TEN,
            BOUGHT,
            quote("10:03:59.5", 70_000, 70_100),
            quote("10:04:00.2", 70_300, 70_400),  # time: sold at 70,000
            Read(on("2026-10-15", "10:00:02.3"), [Barrier(on("2026-10-15", "10:00:00"))]),
            quote(on("2026-10-15", "10:00:02.5"), 70_050, 70_100),  # −6,609.925 in all: within
            quote(on("2026-10-15", "10:01:00"), 69_900, 70_000),  # −8,705.41: reached
            *next_day("2026-10-16"),
            AskResume(on("2026-10-16", "10:01:00")),  # not paused: nothing to resume
            AskRearm(on("2026-10-16", "10:02:00")),
            *next_day("2026-10-16", "10:05"),
        ]
    )

    third, second, first = trades
    assert first["exit"]["reason"] == "time" and first["net"] == pytest.approx(-3_654.21)
    assert second["exit"] == {"at": "2026-10-15T10:01:00+09:00", "price": 69_900, "reason": CUMULATIVE_LIMIT}
    assert ("2026-10-16T10:00", CUMULATIVE_LIMIT) in reasons(day), "the next day too"
    assert third["entry"]["at"] == "2026-10-16T10:05:02.500000+09:00", "re-armed"
    nothing, rearm = day.asked
    assert nothing == "the trader is not paused: nothing to resume"
    assert isinstance(rearm, Control) and (rearm.action, rearm.mode) == (REARM, "paper")
    cumulative, rearmed = day.notices
    assert "누적 손실 한도에 닿았습니다" in cumulative and "-8,705원" in cumulative
    assert "krx-quant trade rearm" in cumulative
    assert "다시 켰습니다" in rearmed and "다시 삽니다" in rearmed
    state = day.state_now(on("2026-10-16", "10:05:03"))
    assert state["status"] == RUNNING
    assert state["cumulative"]["since"] == "2026-10-16T10:02:00+09:00"
    assert state["cumulative"]["realized"] == 0, "counted again from the re-arm"
    assert state["limits"]["cumulative"]["reached_at"] is None
    by_day = {d["day"]: (d["status"], d["halt"]) for d in day.days()}
    assert by_day == {
        "2026-10-14": (RUNNING, None),
        "2026-10-15": (CUMULATIVE_LIMIT, CUMULATIVE_LIMIT),
        "2026-10-16": (RUNNING, None),
    }


def test_a_re_arm_is_refused_unless_the_cumulative_limit_holds(tmp_path: Path) -> None:
    day = Day(tmp_path, rules=rules(daily=0.5))
    day.run([TEN, BOUGHT, quote("10:01:30", 69_900, 70_000), AskRearm("10:02:00")])

    assert day.asked == ["the paper trader has not reached its cumulative loss limit: nothing to re-arm"]
    assert day.state_now("10:02:01")["status"] == DAILY_LIMIT, "nor does it lift the daily limit"


def test_pausing_new_entries_lets_the_open_position_sell_by_its_rules_until_resumed(tmp_path: Path) -> None:
    day = Day(tmp_path)
    first, second = day.run(
        [
            TEN,
            BOUGHT,
            AskPause("10:01:00"),
            quote("10:02:00", 70_300, 70_400),  # the target: sold by its own rule
            Read("10:05:02.3", [Barrier("10:05:00")]),
            quote("10:05:02.5", 70_000, 70_100),
            AskResume("10:06:00"),
            Read("10:10:02.3", [Barrier("10:10:00")]),
            quote("10:10:02.5", 70_000, 70_100),
        ]
    )[::-1]

    assert first["exit"]["reason"] == "take_profit"
    assert reasons(day)[1] == ("2026-10-14T10:05", ENTRIES_PAUSED)
    assert second["bar_close"] == "2026-10-14T10:10:00+09:00", "resumed"
    paused, resumed = day.notices
    assert "새 진입 정지 요청을 받았습니다(화면)" in paused and "원래 규칙" in paused
    assert "krx-quant trade resume" in paused
    assert "정지를 풀었습니다" in resumed and "다시 삽니다" in resumed


def test_pausing_and_selling_now_sells_at_the_next_quote_and_holds_until_resumed(tmp_path: Path) -> None:
    """The quote before the pause is not the sale's: the first after it is. The pause holds into the
    next day: only a resume lifts it."""
    day = Day(tmp_path)
    first, second = day.run(
        [
            TEN,
            BOUGHT,
            quote("10:00:59", 70_200, 70_300),
            AskPause("10:01:00", sell_now=True),
            quote("10:01:00.5", 70_000, 70_100),
            *next_day("2026-10-15"),
            AskResume(on("2026-10-15", "10:02:00")),
            *next_day("2026-10-15", "10:05"),
        ]
    )[::-1]

    assert first["exit"] == {"at": "2026-10-14T10:01:00.500000+09:00", "price": 70_000, "reason": "pause"}
    assert [(o["purpose"], o["status"]) for o in first["orders"]] == [
        ("entry", "filled"),
        ("take_profit", "cancelled"),
        ("pause", "filled"),
    ]
    assert ("2026-10-15T10:00", PAUSED) in reasons(day)
    assert second["bar_close"] == "2026-10-15T10:05:00+09:00"
    assert "정지하고 즉시 매도 요청을 받았습니다(화면)" in day.notices[0]


def test_a_second_pause_selling_now_sells_a_position_the_first_let_run(tmp_path: Path) -> None:
    day = Day(tmp_path)
    [trade] = day.run(
        [
            TEN,
            BOUGHT,
            AskPause("10:01:00"),
            quote("10:01:10", 70_100, 70_200),
            AskPause("10:01:20", sell_now=True),
            quote("10:01:30", 70_100, 70_200),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:01:30+09:00", "price": 70_100, "reason": "pause"}
    pause = day.state_now("10:01:31")["pause"]
    assert pause == {"at": "2026-10-14T10:01:00+09:00", "sell_now": True, "source": "api"}


def test_a_pause_gives_up_a_buy_waiting_for_its_quote(tmp_path: Path) -> None:
    day = Day(tmp_path)

    assert day.run([TEN, AskPause("10:00:02.4"), BOUGHT]) == []
    [decision] = day.decisions()
    assert (decision["action"], decision["reason"]) == ("buy", ENTRIES_PAUSED)


def test_a_restarted_trader_keeps_the_limit_reached_and_sells_what_a_pause_asked_to(tmp_path: Path) -> None:
    """Stopped right after a pause selling now, before a quote came: started again at 10:02, it
    sells at the first quote. A daily limit reached before the restart holds after it."""
    paused = tmp_path / "paused"
    Day(paused).run([TEN, BOUGHT, AskPause("10:01:00", sell_now=True)])
    [trade] = Day(paused, started_at=at("10:02:00")).run([quote("10:02:01", 70_000, 70_100)])
    assert trade["exit"] == {"at": "2026-10-14T10:02:01+09:00", "price": 70_000, "reason": "pause"}

    halted = tmp_path / "halted"
    Day(halted, rules=rules(daily=0.5)).run([TEN, BOUGHT, quote("10:01:30", 69_900, 70_000)])
    again = Day(halted, rules=rules(daily=0.5), started_at=at("11:00:00"))
    again.run([Read("11:00:02.3", [Barrier("11:00:00")]), quote("11:00:02.5", 70_000, 70_100)])
    assert reasons(again)[-1] == ("2026-10-14T11:00", DAILY_LIMIT)


def test_a_limit_narrowed_below_the_loss_realized_holds_from_the_start(tmp_path: Path) -> None:
    """−3,654.21 realized at 10:04 under the default limits; started again with the daily limit
    narrowed to 0.3% (3,000 won), it buys nothing more that day."""
    Day(tmp_path).run([TEN, BOUGHT, quote("10:03:59.5", 70_000, 70_100), Tick("10:04:01")])
    again = Day(tmp_path, rules=rules(daily=0.3), started_at=at("10:30:00"))
    again.run([Read("10:30:02.3", [Barrier("10:30:00")]), quote("10:30:02.5", 70_000, 70_100)])

    assert reasons(again)[-1] == ("2026-10-14T10:30", DAILY_LIMIT)
    assert "하루 손실 한도에 닿았습니다" in again.notices[0]


def test_the_state_shows_the_position_its_bid_the_cash_and_the_limits_used(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.run([TEN, BOUGHT, quote("10:01:00", 69_900, 70_000)])

    state = day.state_now("10:01:05")
    assert (state["mode"], state["status"], state["capital"], state["alive"]) == (
        "paper",
        RUNNING,
        1_000_000,
        True,
    )
    position = state["position"]
    assert (position["quantity"], position["entry_price"], position["bid"]) == (14, 70_100, 69_900)
    assert position["unrealized"] == pytest.approx(-5_051.2) and position["selling"] is False
    assert state["cash"] == pytest.approx(1_000_000 - 70_100 * 14 - 147.21)
    assert state["today"]["net"] == pytest.approx(-5_051.2)
    assert state["limits"]["daily"] == {
        "percent": 10.0,
        "won": 100_000,
        "used": pytest.approx(0.050512),
        "reached_at": None,
    }
    assert state["pause"] is None
    assert day.state_now("10:01:21")["alive"] is False, "a state older than 15 s: the trader is not running"


def test_a_buy_takes_no_more_than_the_paper_account_s_cash() -> None:
    """980,000 won of cash pays 979,853 of notional with its 0.015% commission: 13 shares at 70,100,
    where the capital alone buys 14."""
    assert shares(1.0, 1_000_000, 70_100) == 14
    assert shares(1.0, 1_000_000, 70_100, cash=980_000, fee_rate=FEE) == 13
    assert shares(1.0, 1_000_000, 70_100, cash=-5_000, fee_rate=FEE) == 0


def test_the_controls_are_read_within_a_second() -> None:
    """#393: the engine writes a pause into the trader's store and the trader reads it within 1 s."""
    assert CONTROL_SECONDS <= 0.5


def test_the_rules_carry_the_default_limits() -> None:
    assert (Rules().limits.daily, Rules().limits.cumulative) == (100_000, 200_000)
    assert replace(Rules(), capital=500_000).limits.cumulative == 100_000


@pytest.mark.parametrize("clock_first", [True, False])
def test_an_exit_on_a_day_the_clock_has_not_reached_yet_is_counted_once(
    tmp_path: Path, clock_first: bool
) -> None:
    """A 30-minute barrier bought at 10:00 and still open when the process is suspended; it wakes on
    the 15th to a quote before its clock moves there. The time exit at that bid (70,000: −3,654.21)
    is the day's one trade, whether the quote or the clock rolls the day first."""
    day = Day(tmp_path, cell=replace(Day(tmp_path).cell, kind="barrier_30m"), clock_first=clock_first)
    [trade] = day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", kind="barrier_30m")]),
            BOUGHT,
            quote(on("2026-10-15", "09:00:05"), 70_000, 70_100),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-15T09:00:05+09:00", "price": 70_000, "reason": "time"}
    state = day.state_now(on("2026-10-15", "09:00:06"))
    assert (state["today"]["day"], state["today"]["trades"]) == ("2026-10-15", 1)
    assert state["today"]["realized"] == pytest.approx(-3_654.21)
    assert state["cumulative"]["realized"] == pytest.approx(-3_654.21)
    assert state["cash"] == pytest.approx(1_000_000 - 3_654.21)
    [fifteenth] = [d for d in day.days() if d["day"] == "2026-10-15"]
    assert (fifteenth["trades"], fifteenth["net"]) == (1, pytest.approx(-3_654.21))


def test_the_limits_count_the_champion_s_trades_alone(tmp_path: Path) -> None:
    """#399, #400: a trade entered before the first champion and one of policy 1, the champion,
    count; policy 2's, a challenger's paper trade beside it, does not. Each loses 115.525 (10 shares
    bought at 70,100 and sold at 70,250: 1,500 less 1,615.525 of costs)."""
    store = TraderStore(tmp_path / DB_NAME)
    try:
        trade(store, "paper", at("10:00:00"), at("10:02:00"), 1)
        db = policies.connect(tmp_path / DB_NAME)
        try:
            policies.add(db, at("22:31:00"), "밤 맞추기", [], [], {})  # the champion
            policies.add(db, at("22:31:00") + timedelta(days=1), "밤 맞추기", [], [], {})  # a challenger
        finally:
            db.close()
        morning = at("10:00:00") + timedelta(days=2)
        trade(store, "paper", morning, morning + timedelta(minutes=2), 2, policy_id=1)
        trade(
            store,
            "paper",
            morning + timedelta(minutes=1),
            morning + timedelta(minutes=3),
            3,
            policy_id=2,
            champion=False,
        )
        guard = Guard(store, "paper", Limits(1_000_000, 10, 20), morning + timedelta(hours=1), lambda _: None)
    finally:
        store.close()

    assert guard.realized_day == pytest.approx(-115.525) and guard.trades_day == 1
    assert guard.realized_cumulative == pytest.approx(-231.05) == guard.realized_all
