"""A trade's loss budget and the risk rules (#471, spec #468 4 "크기", 7 "위험 규칙"): the size, the
cash and the capital it is held to, the losses in a row and their cooldown, the buy orders a day, a
restart, the settings that may tighten them alone, and the challengers' and the shadows' books
simulating them with the loss limits.

The 4-minute barrier throughout (``trader_doubles.Barrier``): the reference 70,050 won, tick 100, w 2:
the target 70,250, the stop 69,850. Bought at 70,100, a share loses 70,100 − 69,850 + 70,100 × 0.015%
+ 69,850 × (0.015% + 0.20%) + one tick (the stop sells a tick under it at worst) = 250 + 10.515 +
150.1775 + 100 = 510.6925 won at its stop (``at_risk``): 0.5% of 1,000,000 won, 5,000 won, pays nine.
Bought at 70,000 a share loses 410.6775 won: twelve. Every price is made up.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import pytest

from krx_quant.config import SettingsError
from krx_quant.trader import MOCK
from krx_quant.trader.limits import RUNNING, Limits, Simulated
from krx_quant.trader.paper import (
    BAD_STOP,
    BUY_LIMIT,
    COOLING_DOWN,
    NO_SHARES,
    STOP_LOSS,
    TAKE_PROFIT,
    Rules,
    at_risk,
    midnight,
    risk_held,
    size,
)
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import CUMULATIVE_LIMIT, DAILY_LIMIT, DB_NAME, TraderStore
from tests.trader_doubles import (
    BARRIER,
    FEE,
    AskRearm,
    Barrier,
    Day,
    Event,
    Read,
    at,
    keep,
    policy_cell,
    quote,
)

TRADER = Rules(fee_rate=FEE)  # the trader's rules, its loss budget 0.5% (``trader_doubles``' is the capital)
SHARE = 510.6925  # what a share bought at 70,100 loses at the 69,850 stop
BUDGET = 5_000.0  # 0.5% of 1,000,000 won


def rise(hm: str, day: str = "") -> Read:
    """The barrier's rise of the ``hm`` bar close, read 2.3 s after it."""
    return Read(f"{day}{hm}:02.3", [Barrier(f"{day}{hm}:00")])


def bought(hm: str, ask: float = 70_100, day: str = "") -> BestQuote:
    """The first quote after the ``hm`` rise's read."""
    return quote(f"{day}{hm}:02.5", ask - 100, ask)


def loss(hm: str, day: str = "") -> list[Event]:
    """A rise of the ``hm`` bar close bought at 70,100 and stopped at 69,800 half a minute later."""
    return [rise(hm, day), quote(f"{day}{hm}:02.5", 70_000, 70_100), quote(f"{day}{hm}:30", 69_800, 69_900)]


def win(hm: str, day: str = "") -> list[Event]:
    """A rise of the ``hm`` bar close bought at 70,000 and sold at the target 70,250 half a minute
    later: twelve shares gain 3,000 won less 126 + 1,812.45 of costs."""
    return [rise(hm, day), quote(f"{day}{hm}:02.5", 69_900, 70_000), quote(f"{day}{hm}:30", 70_300, 70_400)]


def reasons(day: Day) -> list[tuple[str, str | None]]:
    """Each decision's bar close (HH:MM) and why it bought nothing (None: it bought)."""
    return [(d["bar_close"][11:16], d["reason"]) for d in day.decisions()]


# The size.


def test_a_share_s_loss_at_its_stop_worked_by_hand() -> None:
    """The ask less the stop, the buy's commission at the ask and the sale's commission and tax at the
    stop (#468: at those prices), and a tick (the stop's band's); the spread not counted again."""
    assert at_risk(70_100, 69_850, FEE) == pytest.approx(SHARE)
    assert at_risk(70_000, 69_850, FEE) == pytest.approx(410.6775)
    # Below 50,000 won the tick is 50.
    assert at_risk(50_100, 49_950, FEE) == pytest.approx(150 + 50_100 * FEE + 49_950 * (FEE + 0.002) + 50)


@pytest.mark.parametrize(
    ("budget", "capital", "cash", "shares"),
    [
        (BUDGET, 1_000_000, 1_000_000, 9),  # ⌊5,000 ÷ 510.6925⌋ = ⌊9.79⌋
        (1_250, 1_000_000, 1_000_000, 2),  # the live first period's 250,000 won: ⌊2.45⌋
        (500, 1_000_000, 1_000_000, NO_SHARES),  # 100,000 won's budget: ⌊0.98⌋, nothing
        (BUDGET, 1_000_000, 300_000, 4),  # the cash: ⌊300,000 ÷ (70,100 × 1.00015)⌋
        (BUDGET, 1_000_000, 140_200, 1),  # two shares at the ask, not with their commission
        (20_000, 1_000_000, 1_000_000, 14),  # the capital: ⌊1,000,000 ÷ 70,110.515⌋
        (BUDGET, 1_000_000, 50_000, NO_SHARES),  # the cash does not pay a share
    ],
)
def test_a_buy_takes_what_its_loss_budget_pays_within_the_cash_and_the_capital(
    budget: float, capital: float, cash: float, shares: int | str
) -> None:
    assert size(70_100, 69_850, budget, capital, cash, FEE) == shares


@pytest.mark.parametrize("stop", [70_100, 70_200, 0, -1, math.nan, math.inf])
def test_a_stop_not_below_the_ask_buys_nothing(stop: float) -> None:
    """Bought at or under its stop, a buy would be stopped out at once."""
    assert size(70_100, stop, BUDGET, 1_000_000, 1_000_000, FEE) == BAD_STOP


def test_a_practice_pick_buys_its_shares_within_the_cash() -> None:
    """The mock practice's one share (#427), whatever the budget; the cash still holds it."""
    assert size(70_100, 69_850, 0.0, 1_000_000, 1_000_000, FEE, quantity=1) == 1
    assert size(70_100, 69_850, BUDGET, 1_000_000, 70_000, FEE, quantity=1) == NO_SHARES
    assert size(70_100, 70_100, BUDGET, 1_000_000, 1_000_000, FEE, quantity=1) == BAD_STOP


def test_the_trader_buys_what_its_loss_budget_pays(tmp_path: Path) -> None:
    [trade] = Day(tmp_path, rules=TRADER).run([rise("10:00"), bought("10:00")])

    assert trade["entry"]["quantity"] == 9
    assert trade["fraction"] is None and trade["kelly"] > 1, "f* kept, a diagnosis alone: no fraction bought"


def test_env_narrows_the_loss_budget_and_the_capital_narrows_it_too(tmp_path: Path) -> None:
    """0.25% of 1,000,000 won pays four; 0.5% of 100,000 won (500) pays none: ``no_shares``."""
    narrowed = Day(tmp_path / "narrowed", rules=Rules(fee_rate=FEE, risk_percent=0.25))
    [trade] = narrowed.run([rise("10:00"), bought("10:00")])
    assert trade["entry"]["quantity"] == 4

    small = Day(tmp_path / "small", rules=Rules(fee_rate=FEE, capital=100_000))
    assert small.run([rise("10:00"), bought("10:00")]) == []
    assert reasons(small) == [("10:00", NO_SHARES)]


def test_a_buy_whose_ask_fell_through_its_stop_buys_nothing(tmp_path: Path) -> None:
    day = Day(tmp_path, rules=TRADER)

    assert day.run([rise("10:00"), quote("10:00:02.5", 69_700, 69_800)]) == []
    assert reasons(day) == [("10:00", BAD_STOP)]


# The losses in a row.


def test_risk_held_worked_by_hand() -> None:
    rules = Rules()
    t = at("10:00:00")
    three = [(t, -1.0), (t + timedelta(minutes=5), -1.0), (t + timedelta(minutes=10), -1.0)]
    last = three[-1][0]
    assert risk_held(rules, last + timedelta(minutes=29, seconds=59), 3, three) == COOLING_DOWN
    assert risk_held(rules, last + timedelta(minutes=30), 3, three) is None, "30 minutes from the third"
    assert risk_held(rules, last, 3, three[:2]) is None, "two"
    broken = [three[0], (three[1][0], 0.0), three[2]]
    assert risk_held(rules, last, 3, broken) is None, "a net of 0 is no loss: the row ends"
    fourth = [*three, (last + timedelta(minutes=40), -1.0)]
    assert risk_held(rules, last + timedelta(minutes=45), 4, fourth) == COOLING_DOWN, "each loss past three"
    assert risk_held(rules, t, 19, []) is None
    assert risk_held(rules, t, 20, []) == BUY_LIMIT
    tight = Rules(loss_streak=2, cooldown=timedelta(minutes=45), daily_buys=5)
    assert risk_held(tight, three[1][0] + timedelta(minutes=44), 2, three[:2]) == COOLING_DOWN
    assert risk_held(tight, t, 5, []) == BUY_LIMIT


def test_three_net_losses_in_a_row_stop_new_buys_for_thirty_minutes(tmp_path: Path) -> None:
    """Stopped at 10:00:30, 10:10:30 and 10:20:30: the 10:30 and 10:50 rises are cooling down; the
    10:51 one, read past 10:50:30, buys."""
    day = Day(tmp_path, rules=TRADER)

    trades = day.run(
        [*loss("10:00"), *loss("10:10"), *loss("10:20"), rise("10:30"), rise("10:50"), *win("10:51")]
    )

    assert [t["exit"]["reason"] for t in reversed(trades)] == [STOP_LOSS] * 3 + [TAKE_PROFIT]
    assert reasons(day) == [
        ("10:00", None),
        ("10:10", None),
        ("10:20", None),
        ("10:30", COOLING_DOWN),
        ("10:50", COOLING_DOWN),
        ("10:51", None),
    ]


def test_a_gain_ends_the_row(tmp_path: Path) -> None:
    day = Day(tmp_path, rules=TRADER)

    trades = day.run([*loss("10:00"), *loss("10:10"), *win("10:20"), *loss("10:30"), *win("10:40")])

    assert len(trades) == 5 and all(reason is None for _, reason in reasons(day))


def test_the_cooldown_goes_on_across_a_restart(tmp_path: Path) -> None:
    Day(tmp_path, rules=TRADER).run([*loss("10:00"), *loss("10:10"), *loss("10:20")])

    again = Day(tmp_path, rules=TRADER, started_at=at("10:25:00"))
    again.run([rise("10:30"), bought("10:30")])

    assert reasons(again)[-1] == ("10:30", COOLING_DOWN)


# The buy orders a day.


def twenty_buys() -> list[Event]:
    return [e for m in range(20) for e in win(f"10:{m:02d}")]


def test_twenty_buys_a_day_and_no_more(tmp_path: Path) -> None:
    """Twenty gains from 10:00, a minute apart; the 10:20 rise buys nothing. The next day counts again
    from 0."""
    day = Day(tmp_path, rules=TRADER)

    trades = day.run([*twenty_buys(), *win("10:20"), *win("10:00", day="2026-10-15 ")])

    assert len(trades) == 21
    assert reasons(day)[20:] == [("10:20", BUY_LIMIT), ("10:00", None)]


def test_the_day_s_buys_go_on_across_a_restart(tmp_path: Path) -> None:
    Day(tmp_path, rules=TRADER).run(twenty_buys())

    again = Day(tmp_path, rules=TRADER, started_at=at("10:25:00"))
    again.run(win("10:30"))

    assert reasons(again)[-1] == ("10:30", BUY_LIMIT)


def test_a_kis_book_counts_each_buy_order_it_sent_and_no_sell(tmp_path: Path) -> None:
    """The KIS book's buy orders (``unfilled_buys``, kept before each is sent): one filled, one KIS
    took of which nothing filled, one it refused, one whose answer was lost; its sells (the trades'
    take-profit and stop) are not buys. A paper book counts its fills."""
    store = TraderStore(tmp_path / DB_NAME)
    try:
        for status in ("filled", "cancelled", "refused", "sent"):
            store.db.execute(
                "INSERT INTO unfilled_buys (decision_id, mode, price, quantity, placed_at, status, broker_id)"
                " VALUES (1, ?, 70100, 9, ?, ?, '')",
                (MOCK, at("10:00:02.5").isoformat(), status),
            )
        store.db.execute(
            "INSERT INTO unfilled_buys (decision_id, mode, price, quantity, placed_at, status, broker_id)"
            " VALUES (1, ?, 70100, 9, ?, 'filled', '')",
            (MOCK, at("2026-10-13 10:00:02.5").isoformat()),
        )
        store.db.commit()
        since = midnight(at("10:30:00"))
        assert store.day_buys(MOCK, since) == 4, "today's four; the day before's not"
        assert store.day_buys("paper", since) == 0, "a paper book counts its trades"
    finally:
        store.close()


# The challengers and the shadows: the same size, risk rules and loss limits, simulated.


GAP: list[Event] = [
    # Bought nine at 70,100; the bid gaps to 59,500 under the stop: stopped there, −96,645.96 won.
    rise("10:00"),
    quote("10:00:02.5", 70_000, 70_100),
    quote("10:01:00", 59_500, 59_600),
    # Bought nine again; the bid at 69,860, above the stop 69,850, opens −3,606.43: the day's −100,252
    # reaches the daily limit (100,000 won), sold there at once.
    rise("10:10"),
    quote("10:10:02.5", 70_000, 70_100),
    quote("10:11:00", 69_860, 69_960),
    rise("10:20"),
    quote("10:20:02.5", 70_000, 70_100),
]


def test_a_challenger_sizes_by_the_loss_budget_and_its_loss_limit_holds_it_as_the_champion_s(
    tmp_path: Path,
) -> None:
    """The champion and a challenger alike (θ 0.6): both buy nine, both are sold at 69,860 by the
    daily limit before the stop, and neither buys the 10:20 rise. The challenger's limit is its own,
    simulated: no halt is kept for it, and the champion's day counts the champion's trades alone."""
    assert keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(BARRIER, 0.6)) == 1
    assert keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6)) == 2
    day = Day(tmp_path, desk=True, rules=TRADER)

    champion = day.run(GAP)

    challenger = day.trades(policy=2)
    for trades in (champion, challenger):
        assert [
            (t["entry"]["quantity"], t["exit"]["reason"], t["exit"]["price"]) for t in reversed(trades)
        ] == [
            (9, STOP_LOSS, 59_500),
            (9, DAILY_LIMIT, 69_860),
        ]
    assert sum(t["net"] for t in challenger) == pytest.approx(-96_645.96 - 3_606.426)
    skipped = [(d["policy_id"], d["reason"]) for d in day.decisions() if d["bar_close"][11:16] == "10:20"]
    assert skipped == [(1, DAILY_LIMIT), (2, DAILY_LIMIT)]
    state = day.state_now("10:20:02.5")
    assert state["today"]["realized"] == pytest.approx(sum(t["net"] for t in champion))


def test_a_challenger_s_cumulative_limit_is_rearmed_with_the_trader_s(tmp_path: Path) -> None:
    """The cumulative limit narrowed to 9% (90,000 won): the gap's loss reaches it for the champion and
    the challenger alike; the trader re-armed at 10:05 re-arms the challenger's simulated one too, and
    both buy the 10:10 rise."""
    assert keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(BARRIER, 0.6)) == 1
    assert keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6)) == 2
    day = Day(tmp_path, desk=True, rules=Rules(fee_rate=FEE, cumulative_loss_percent=9.0))

    day.run(
        [
            *GAP[:3],
            rise("10:03"),
            AskRearm("10:05:00"),
            rise("10:10"),
            quote("10:10:02.5", 70_000, 70_100),
            quote("10:11:00", 70_300, 70_400),
        ]
    )

    assert [(d["policy_id"], d["bar_close"][11:16], d["reason"]) for d in day.decisions()] == [
        (1, "10:00", None),
        (2, "10:00", None),
        (1, "10:03", CUMULATIVE_LIMIT),
        (2, "10:03", CUMULATIVE_LIMIT),
        (1, "10:10", None),
        (2, "10:10", None),
    ]


def test_the_simulated_loss_limits_worked_by_hand() -> None:
    limits = Limits(1_000_000, 10.0, 20.0)
    held = Simulated(limits, at("10:00:00").date())
    assert held.mark(-99_999.0, at("10:01:00")) is None and held.status == RUNNING
    assert held.mark(-100_000.0, at("10:01:01")) == DAILY_LIMIT, "at its negative"
    assert held.exited(-150_000.0, at("10:01:02")) is None and held.status == DAILY_LIMIT
    assert held.roll(at("2026-10-15 09:00:00")) and held.status == RUNNING, "a new day"
    assert held.exited(-60_000.0, at("2026-10-15 10:00:00")) == CUMULATIVE_LIMIT
    assert held.roll(at("2026-10-16 09:00:00")) and held.status == CUMULATIVE_LIMIT, "until re-armed"
    held.rearm()
    assert held.status == RUNNING and held.realized_cumulative == 0.0
    # A loss realized past a limit before the start holds from it.
    assert Simulated(limits, at("10:00:00").date(), -100_000.0, -100_000.0).status == DAILY_LIMIT
    # A book's new day reads that day's P&L off its trades, as the champion's guard reads the store.
    read = Simulated(limits, at("10:00:00").date(), day_realized=lambda day: -120_000.0)
    assert read.roll(at("2026-10-15 09:00:00")) and (read.realized_day, read.status) == (
        -120_000.0,
        DAILY_LIMIT,
    )


# The settings.


def test_the_risk_rules_default_to_the_code_s_and_env_tightens_them() -> None:
    default = TraderSettings.from_env({}, None)
    assert (
        default.risk_per_trade_percent,
        default.loss_streak,
        default.cooldown_minutes,
        default.daily_buy_orders,
    ) == (0.5, 3, 30.0, 20)
    tight = TraderSettings.from_env(
        {
            "KRX_TRADER_RISK_PER_TRADE_PERCENT": "0.25",
            "KRX_TRADER_LOSS_STREAK": "2",
            "KRX_TRADER_COOLDOWN_MINUTES": "60",
            "KRX_TRADER_DAILY_BUY_ORDERS": "10",
        },
        None,
    )
    rules = Rules.of(tight, FEE)
    assert (rules.risk_percent, rules.loss_streak, rules.cooldown, rules.daily_buys) == (
        0.25,
        2,
        timedelta(minutes=60),
        10,
    )
    assert rules.budget(250_000) == pytest.approx(625.0)


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("KRX_TRADER_RISK_PER_TRADE_PERCENT", "0.6", "never widen"),
        ("KRX_TRADER_RISK_PER_TRADE_PERCENT", "0", "above 0"),
        ("KRX_TRADER_RISK_PER_TRADE_PERCENT", "half", "a number"),
        ("KRX_TRADER_LOSS_STREAK", "4", "never raise"),
        ("KRX_TRADER_LOSS_STREAK", "0", "KRX_TRADER_LOSS_STREAK"),
        ("KRX_TRADER_LOSS_STREAK", "2.5", "a number"),
        ("KRX_TRADER_DAILY_BUY_ORDERS", "21", "never raise"),
        ("KRX_TRADER_DAILY_BUY_ORDERS", "0", "KRX_TRADER_DAILY_BUY_ORDERS"),
        ("KRX_TRADER_COOLDOWN_MINUTES", "29", "never shorten"),
        ("KRX_TRADER_COOLDOWN_MINUTES", "inf", "KRX_TRADER_COOLDOWN_MINUTES"),
    ],
)
def test_env_that_loosens_a_risk_rule_stops_the_trader(name: str, value: str, why: str) -> None:
    with pytest.raises(SettingsError, match=why):
        TraderSettings.from_env({name: value}, None)
