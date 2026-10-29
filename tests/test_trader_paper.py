"""Paper trading by the triple barrier (#394), on made-up quotes and predictions stored in an engine
database, as the trader reads them.

The example used throughout: a barrier prediction of the 10:00 bar close, displayed at 10:00:02
and read at 10:00:02.3, of the rise (up_first 0.7). Its reference is the mid-point 70,050
(tick 100) and its width w = 2: the target is 70,250, the stop 69,850, the time barrier 10:04.
The first quote after the decision (10:00:02.5) asks 70,100: 1,000,000 ÷ 70,100 buys 14 shares.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from krx_quant.barriers import TIMEOUT as BARRIER_TIMEOUT
from krx_quant.barriers import UP_FIRST, barrier_answer
from krx_quant.features import Width
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import QuoteLog
from krx_quant.pricing import cost_ticks
from krx_quant.trader import SYMBOL, TRADED_KINDS
from krx_quant.trader.cells import traded_kind
from krx_quant.trader.paper import (
    BEFORE_ENTRY,
    BELOW_THRESHOLD,
    BLOCKED,
    EXPIRED,
    HOLDING,
    NO_QUOTE,
    NO_REFERENCE,
    NO_SHARES,
    NOT_OK,
    NOT_UP,
    PAST_LAST_EXIT,
    RESTARTED,
    STALE,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_REACHED,
    TIME,
    Rules,
    costs,
)
from tests.trader_doubles import DIRECTION, FEE, Barrier, Day, Direction, Event, Read, Tick, at, quote

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)


def test_a_rise_buys_at_the_first_ask_after_the_decision_and_takes_profit_at_the_target(
    tmp_path: Path,
) -> None:
    [trade] = Day(tmp_path).run(
        [
            quote("10:00:01", 69_900, 70_000),  # before the decision: not the buy's
            TEN,
            BOUGHT,
            quote("10:01:00", 70_200, 70_300),  # the bid below the target
            quote("10:02:00", 70_300, 70_400),  # at or above it: sold at the target
            quote("10:03:00", 70_500, 70_600),  # after the exit: nothing
        ]
    )

    assert (trade["target"], trade["stop"], trade["time_barrier"]) == (
        70_250,
        69_850,
        "2026-10-14T10:04:00+09:00",
    )
    assert trade["entry"] == {"at": "2026-10-14T10:00:02.500000+09:00", "price": 70_100, "quantity": 14}
    assert trade["exit"] == {"at": "2026-10-14T10:02:00+09:00", "price": 70_250, "reason": TAKE_PROFIT}
    assert (trade["mode"], trade["kind"], trade["predictor"], trade["probability"]) == (
        "paper",
        "barrier",
        "breiman",
        0.7,
    )
    assert [(o["side"], o["purpose"], o["price"], o["status"]) for o in trade["orders"]] == [
        ("buy", "entry", 70_100, "filled"),
        ("sell", "take_profit", 70_250, "filled"),  # resting at the target from the buy on
    ]
    assert [len(o["fills"]) for o in trade["orders"]] == [1, 1]
    assert trade["orders"][1]["placed_at"] == trade["entry"]["at"]


def test_the_costs_of_a_round_trip_worked_by_hand() -> None:
    """14 shares bought at 70,100 and sold at 70,250, 0.015% a side and the 0.20% sell tax:
    the buy's commission 981,400 × 0.00015 = 147.21; the sell's 983,500 × 0.00015 = 147.525; the
    tax 983,500 × 0.002 = 1,967. 2,261.735 won in all, against 150 × 14 = 2,100 gained: the
    target of k = 2 ticks, reached from the ask half a tick above the reference, loses 161.735."""
    paid = costs(70_100, 70_250, 14, FEE)

    assert (paid.buy_fee, paid.sell_fee, paid.tax) == pytest.approx((147.21, 147.525, 1_967.0))
    assert paid.total == pytest.approx(2_261.735)
    # The same round trip on the reference price per share, in ticks (``pricing.cost_ticks``, what
    # the touch and barrier P&L take off): 70,050 × 0.23% ÷ 100.
    assert cost_ticks(70_050, FEE) == pytest.approx(1.61115)


def test_a_trade_keeps_its_costs_and_its_net_in_won(tmp_path: Path) -> None:
    [trade] = Day(tmp_path).run([TEN, BOUGHT, quote("10:02:00", 70_300, 70_400)])

    assert (trade["buy_fee"], trade["sell_fee"], trade["tax"]) == pytest.approx((147.21, 147.525, 1_967.0))
    assert trade["cost"] == pytest.approx(2_261.735)
    assert trade["gross"] == pytest.approx(2_100)
    assert trade["net"] == pytest.approx(-161.735)


def test_a_bid_at_or_below_the_stop_sells_at_that_bid(tmp_path: Path) -> None:
    """(69,800 − 70,100) × 14 = −4,200; the costs 147.21 + 146.58 (977,200 × 0.015%) + 1,954.4."""
    [trade] = Day(tmp_path).run(
        [
            TEN,
            BOUGHT,
            quote("10:01:00", 69_900, 70_000),  # above the stop (69,850)
            quote("10:01:30", 69_800, 69_900),  # at or below it
            quote("10:02:00", 70_300, 70_400),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:01:30+09:00", "price": 69_800, "reason": STOP_LOSS}
    assert trade["net"] == pytest.approx(-4_200 - 147.21 - 146.58 - 1_954.4)
    assert [(o["purpose"], o["price"], o["status"]) for o in trade["orders"]] == [
        ("entry", 70_100, "filled"),
        ("take_profit", 70_250, "cancelled"),  # taken off for the stop's sell
        ("stop_loss", 69_800, "filled"),
    ]


def test_the_time_barrier_sells_at_the_last_bid_before_it(tmp_path: Path) -> None:
    [trade] = Day(tmp_path).run(
        [
            TEN,
            BOUGHT,
            quote("10:02:00", 70_100, 70_200),
            quote("10:03:59.5", 70_000, 70_100),  # the book standing at 10:04
            quote("10:04:00.2", 70_300, 70_400),  # the first after it: its bid is not the sale's
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:04:00+09:00", "price": 70_000, "reason": TIME}
    assert [(o["purpose"], o["status"]) for o in trade["orders"]] == [
        ("entry", "filled"),
        ("take_profit", "cancelled"),
        ("time", "filled"),
    ]
    assert trade["orders"][2]["fills"][0]["quote_at"] == "2026-10-14T10:03:59.500000+09:00"


def test_the_clock_sells_at_the_time_barrier_without_a_quote_after_it(tmp_path: Path) -> None:
    """Quotes received before 10:04 may still be on their way: the clock waits a second past it."""
    events: list[Event] = [TEN, BOUGHT, quote("10:03:59.5", 70_000, 70_100)]

    assert Day(tmp_path / "a").run([*events, Tick("10:04:00.5")])[0]["exit"] is None, "within the grace"
    [trade] = Day(tmp_path / "b").run([*events, Tick("10:04:01")])

    assert trade["exit"] == {"at": "2026-10-14T10:04:00+09:00", "price": 70_000, "reason": TIME}


def test_without_a_recent_quote_before_the_barrier_the_time_exit_waits_for_the_next(tmp_path: Path) -> None:
    """The stream was down from 10:01: the sale is at the first quote back, and its time shows the wait."""
    [trade] = Day(tmp_path).run(
        [
            TEN,
            BOUGHT,
            quote("10:01:00", 70_100, 70_200),
            Tick("10:04:01"),
            Tick("10:05:00"),
            quote("10:06:10", 69_900, 70_000),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:06:10+09:00", "price": 69_900, "reason": TIME}
    assert trade["time_barrier"] == "2026-10-14T10:04:00+09:00"


def test_no_buy_when_the_ask_has_reached_the_target_already(tmp_path: Path) -> None:
    day = Day(tmp_path)
    trades = day.run([TEN, quote("10:00:02.5", 70_200, 70_300)])  # the ask above 70,250

    assert trades == []
    [decision] = day.decisions()
    assert (decision["action"], decision["reason"], decision["trade_id"]) == ("buy", TARGET_REACHED, None)


def test_no_entry_whose_time_barrier_is_past_15_15(tmp_path: Path) -> None:
    """15:15 is five minutes before the closing auction (15:20): a 30-minute barrier of the 14:45 bar
    close ends there and is taken; the 14:46 one's ends at 15:16 and is not. So is a 4-minute
    barrier of 15:12, while 15:11's ends at 15:15."""
    late: list[Event] = [
        Read("14:45:02.3", [Barrier("14:45:00", kind="barrier_30m")]),
        Read("14:46:02.3", [Barrier("14:46:00", kind="barrier_30m")]),
    ]
    thirty = Day(tmp_path / "30m", cell=replace(Day(tmp_path).cell, kind="barrier_30m"))
    [trade] = thirty.run([*late, quote("14:45:02.5", 70_000, 70_100)])
    assert trade["time_barrier"] == "2026-10-14T15:15:00+09:00"
    assert [(d["bar_close"][11:16], d["reason"]) for d in thirty.decisions()] == [
        ("14:45", None),
        ("14:46", PAST_LAST_EXIT),  # held too, but a prediction's own reason comes first
    ]

    alone = Day(tmp_path / "alone", cell=thirty.cell)
    assert alone.run([late[1], quote("14:46:02.5", 70_000, 70_100)]) == []
    assert [d["reason"] for d in alone.decisions()] == [PAST_LAST_EXIT]

    four = Day(tmp_path / "4m")
    four.run(
        [
            Read("15:11:02.3", [Barrier("15:11:00")]),
            quote("15:11:02.5", 70_000, 70_100),
            quote("15:13:00", 70_300, 70_400),  # sold: free again
            Read("15:12:02.3", [Barrier("15:12:00")]),
            quote("15:12:02.5", 70_000, 70_100),
        ]
    )
    assert [d["reason"] for d in four.decisions()] == [None, PAST_LAST_EXIT]


def test_no_entry_once_the_time_barrier_has_passed(tmp_path: Path) -> None:
    """An engine catching up displays an overdue bar close's prediction late: the 15:11 barrier's,
    whose time barrier is 15:15, displayed at 15:20 during the closing auction, buys nothing (#409).
    Nor does a buy decided just before its barrier whose first quote comes at or after it."""
    late = Day(tmp_path / "late")
    assert (
        late.run([Read("15:20:00.3", [Barrier("15:11:00", shown=540)]), quote("15:20:00.5", 70_000, 70_100)])
        == []
    )
    assert [(d["action"], d["reason"]) for d in late.decisions()] == [("skip", EXPIRED)]

    edge = Day(tmp_path / "edge")
    assert (
        edge.run([Read("10:03:58.3", [Barrier("10:00:00", shown=238)]), quote("10:04:00.5", 70_000, 70_100)])
        == []
    )
    assert [(d["action"], d["reason"]) for d in edge.decisions()] == [("buy", EXPIRED)]


def test_no_entry_before_09_05(tmp_path: Path) -> None:
    day = Day(tmp_path)
    [trade] = day.run(
        [
            Read("09:04:02.3", [Barrier("09:04:00")]),
            quote("09:04:02.5", 70_000, 70_100),
            Read("09:05:02.3", [Barrier("09:05:00")]),
            quote("09:05:02.5", 70_000, 70_100),
        ]
    )

    assert trade["bar_close"] == "2026-10-14T09:05:00+09:00"
    assert [(d["bar_close"][11:16], d["action"], d["reason"]) for d in day.decisions()] == [
        ("09:04", "skip", BEFORE_ENTRY),
        ("09:05", "buy", None),
    ]


def test_a_prediction_buys_only_a_likely_enough_rise(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", p=(0.1, 0.2, 0.7))]),  # down first
            Read("10:01:02.3", [Barrier("10:01:00", p=(0.4, 0.4, 0.2))]),  # a tie goes to the middle
            Read("10:02:02.3", [Barrier("10:02:00", p=(0.55, 0.25, 0.2))]),  # below θ = 0.6
            Read("10:03:02.3", [Barrier("10:03:00", status="timed_out")]),
            Read("10:04:02.3", [Barrier("10:04:00", anchor=None)]),  # no mid-point at the bar close
            Read("10:05:30", [Barrier("10:05:00")]),  # read 28 s after its display
            Read("10:06:02.3", [Barrier("10:06:00", predictor="barrier-climate")]),  # not the cell's
        ]
    )

    assert [d["reason"] for d in day.decisions()] == [
        NOT_UP,
        NOT_UP,
        BELOW_THRESHOLD,
        NOT_OK,
        NO_REFERENCE,
        STALE,
    ]


def test_one_position_at_a_time(tmp_path: Path) -> None:
    day = Day(tmp_path)
    trades = day.run(
        [
            TEN,
            BOUGHT,
            Read("10:01:02.3", [Barrier("10:01:00")]),  # held
            quote("10:02:00", 70_300, 70_400),  # sold
            Read("10:03:02.3", [Barrier("10:03:00")]),
            quote("10:03:02.5", 70_000, 70_100),
        ]
    )

    assert [d["reason"] for d in day.decisions()] == [None, HOLDING, None]
    assert [t["bar_close"][11:16] for t in trades] == ["10:03", "10:00"]


def test_a_buy_without_a_quote_within_five_seconds_is_given_up(tmp_path: Path) -> None:
    by_clock = Day(tmp_path / "clock")
    assert by_clock.run([TEN, Tick("10:00:08.3")]) == []  # 5 s and the grace
    late = Day(tmp_path / "late")
    assert late.run([TEN, quote("10:00:07.4", 70_000, 70_100)]) == []
    assert (
        [d["reason"] for d in by_clock.decisions()] == [d["reason"] for d in late.decisions()] == [NO_QUOTE]
    )


def test_a_capital_below_one_share_buys_nothing(tmp_path: Path) -> None:
    day = Day(tmp_path, rules=Rules(fee_rate=FEE, capital=70_000))
    assert day.run([TEN, BOUGHT]) == []
    assert [d["reason"] for d in day.decisions()] == [NO_SHARES]


def test_the_direction_trades_its_final_consensus_on_k(tmp_path: Path) -> None:
    """The direction's target and stop are k ticks from its bar close's mid-point; a blocked final
    consensus (#88) buys nothing."""
    day = Day(tmp_path, cell=DIRECTION)
    [trade] = day.run(
        [
            Read("10:00:02.3", [Direction("10:00:00", blocked="special")]),
            quote("10:00:02.5", 70_000, 70_100),
            Read("10:01:02.3", [Direction("10:01:00")]),
            quote("10:01:02.5", 70_000, 70_100),
            quote("10:03:00", 69_800, 69_900),
        ]
    )

    assert (trade["kind"], trade["predictor"], trade["width_ticks"]) == ("direction", "consensus-final", 2)
    assert (trade["anchor"], trade["tick"], trade["target"], trade["stop"]) == (70_050, 100, 70_250, 69_850)
    assert trade["time_barrier"] == "2026-10-14T10:05:00+09:00", "four minutes"
    assert trade["exit"]["reason"] == STOP_LOSS
    assert [d["reason"] for d in day.decisions()] == [BLOCKED, None]


def test_the_same_inputs_make_the_same_trades(tmp_path: Path) -> None:
    """Twice the same day; once more with the clock ticking at other times: the trades are the same."""
    events: list[Event] = [
        TEN,
        BOUGHT,
        quote("10:01:00", 70_100, 70_200),
        quote("10:02:30", 70_250, 70_350),  # take-profit
        Read("10:05:02.3", [Barrier("10:05:00", kind="barrier")]),
        quote("10:05:02.6", 70_000, 70_100),
        quote("10:08:59.9", 70_000, 70_100),
        quote("10:09:00.1", 69_900, 70_000),  # time exit at 70,000
        Read("10:10:02.3", [Barrier("10:10:00")]),
        quote("10:10:09", 70_000, 70_100),  # too late: no buy
        Read("10:11:02.3", [Barrier("10:11:00")]),
        quote("10:11:03", 70_000, 70_100),
        quote("10:12:00", 69_800, 69_900),  # stop
    ]
    ticks = [Tick(f"10:{m:02d}:{s:02d}") for m in range(0, 15) for s in range(0, 60, 7)]

    first = Day(tmp_path / "first").run(events)
    again = Day(tmp_path / "again").run(events)
    ticking = Day(tmp_path / "ticking").run([*events, *ticks])

    assert [t["exit"]["reason"] for t in first] == [STOP_LOSS, TIME, TAKE_PROFIT]
    assert first == again == ticking


def test_a_restarted_trader_goes_on_with_its_open_trade(tmp_path: Path) -> None:
    Day(tmp_path).run([TEN, BOUGHT, quote("10:01:00", 70_100, 70_200)])
    later = Day(tmp_path, started_at=at("10:02:00"))
    [trade] = later.run(
        [
            quote("10:01:50", 70_300, 70_400),  # the book before the start: no trigger
            quote("10:02:30", 70_300, 70_400),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:02:30+09:00", "price": 70_250, "reason": TAKE_PROFIT}


def test_a_trade_whose_barrier_passed_while_stopped_sells_at_the_first_quote_back(tmp_path: Path) -> None:
    Day(tmp_path).run([TEN, BOUGHT])
    [trade] = Day(tmp_path, started_at=at("10:10:00")).run(
        [quote("10:09:59", 70_000, 70_100), Tick("10:10:05"), quote("10:10:06", 69_900, 70_000)]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:10:06+09:00", "price": 69_900, "reason": TIME}


def test_a_buy_waiting_for_its_quote_when_the_trader_stopped_is_given_up(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.run([TEN])
    assert Day(tmp_path, started_at=at("10:00:30")).run([quote("10:00:31", 70_000, 70_100)]) == []
    assert [(d["action"], d["reason"]) for d in day.decisions()] == [("buy", RESTARTED)]


def test_a_prediction_decided_before_a_restart_is_not_decided_again(tmp_path: Path) -> None:
    day = Day(tmp_path)
    day.run([TEN, BOUGHT, quote("10:02:00", 70_300, 70_400)])
    again = Day(tmp_path, started_at=at("10:00:05")).run(
        [Read("10:00:06"), quote("10:00:07", 70_000, 70_100)]
    )

    assert len(again) == 1 and len(day.decisions()) == 1


def test_the_take_profit_and_the_time_exit_are_the_barrier_answer_s_events(tmp_path: Path) -> None:
    """The same quotes make the barrier answer (``barriers.barrier_answer``) and the paper trade: its
    up_first is the take-profit at U, its timeout the time exit at its last bid (``bid_end``)."""
    rising = [
        quote("10:00:02.5", 70_000, 70_100),
        quote("10:01:00", 70_200, 70_300),
        quote("10:02:00", 70_300, 70_400),
    ]
    flat = [quote("10:00:02.5", 70_000, 70_100), quote("10:03:59.5", 70_050, 70_150)]
    for name, quotes, label, reason in (
        ("rising", rising, UP_FIRST, TAKE_PROFIT),
        ("flat", flat, BARRIER_TIMEOUT, TIME),
    ):
        log = QuoteLog()
        for q in quotes:
            log.quote(q.received_at, q.bid, q.ask, q.bid_size, q.ask_size, rest=False)
        window = log.window(
            SYMBOL,
            at("10:00:00"),
            70_050,
            100,
            2,
            70_000,
            70_100,
            cost=cost_ticks(70_050, FEE),
            width=Width(2, None),
            crossed=True,
            horizon=timedelta(minutes=4),
        )
        answer = barrier_answer(window)
        [trade] = Day(tmp_path / name).run([TEN, *quotes, quote("10:04:00.5", 69_000, 69_100)])

        assert answer.label == label
        assert trade["exit"]["reason"] == reason
        assert trade["exit"]["price"] == (
            answer.path["upper"] if label == UP_FIRST else answer.path["bid_end"]
        )


def test_the_traded_kinds_are_the_engine_s_with_their_horizons() -> None:
    assert [traded_kind(name).horizon for name in TRADED_KINDS] == [
        timedelta(minutes=m) for m in (4, 4, 15, 30)
    ]
    for name in TRADED_KINDS:
        kind = kind_named(name)
        assert kind is not None and kind.sides is not None
        assert kind.index(kind.sides[0]) == 0, "the rise is the first class"
