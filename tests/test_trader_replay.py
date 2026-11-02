"""The recorded days replayed by the paper trader's own rules (#470): the same predictions and the same
recorded quotes make the same trades as the paper trader; the rules the replay used to break (the buy
at the bar close's ask, the stop on the ask, no stop for the direction, the costs on the reference);
the signals of a recording's gap left out and counted; a day's recording held at a time.

Input: the engine database the paper trader read (``tests/trader_doubles.py``'s ``Day``: the engine
displays its predictions, the trader reads them), and the same quotes recorded as the collector
records the integrated book. Output: the paper trader's trades and the replay's, side by side. Every
price is made up.

The 4-minute barrier throughout (``trader_doubles.Barrier``): the reference 70,050 won, tick 100, w 2
(target 70,250, stop 69,850), displayed 2 s after its bar close.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.feeds import BOOK
from krx_quant.feeds.base import KST
from krx_quant.health import GAP_SCHEMA
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import CAPITAL, SYMBOL, replay
from krx_quant.trader.cells import Cell, open_engine_db, special_windows
from krx_quant.trader.paper import (
    COOLING_DOWN,
    EXPIRED,
    HOLDING,
    NO_QUOTE,
    NOT_UP,
    OUTRANKED,
    SPECIAL,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_REACHED,
    TIME,
    Rules,
    net_of,
)
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.replay import (
    GAP,
    NO_EXIT_QUOTE,
    NO_RECORDING,
    OFF_TARGET,
    Pass,
    Ranked,
    Recording,
    Replayer,
    Trade,
)
from krx_quant.trader.selection import Pick
from krx_quant.trader.store import DAILY_LIMIT
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    DAY,
    DIRECTION,
    FEE,
    Barrier,
    Day,
    Direction,
    Event,
    Read,
    Special,
    Tick,
    at,
    pick,
    quote,
    record_quotes,
)

FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
TODAY = date(*DAY)
FALL = (0.2, 0.3, 0.5)  # a barrier prediction whose most likely class is the fall


def record_gap(data: Path, start: datetime, end: datetime | None, feed: str = BOOK.tr_id) -> None:
    """A gap of 005930's ``feed`` kept as the collector keeps it: under the day it started."""
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    row = {"feed": feed, "symbol": SYMBOL, "start": start, "end": end, "reason": "disconnected: test"}
    recorder.record("gaps", start.astimezone(KST).date(), feed, SYMBOL, row, GAP_SCHEMA)
    recorder.close()


def models(state: Path) -> None:
    """The engine's models and the targets they were trained on: breiman's on the barrier's answer."""
    store = PredictionStore(state / ENGINE_DB)
    try:
        store.record_models([(BARRIER.predictor, "v1", BARRIER_VERSION)])
    finally:
        store.close()


def replayed(
    state: Path,
    picks: Sequence[Pick],
    *,
    days: Sequence[date] = (TODAY,),
    capital: int = CAPITAL,
    thresholds: Sequence[float] = (0.6,),
    alone: Cell | None = None,
) -> tuple[Pass, list[Trade], Replayer]:
    """The replay of ``picks`` as one policy (each at its θ, else 0.6), by the trader's rules on
    ``capital``, on the engine database in ``state`` and the recordings under ``state / "data"``; its
    trades."""
    trades: list[Trade] = []
    db = open_engine_db(state / ENGINE_DB)
    try:
        windows = special_windows(db, SYMBOL, at("00:00:00"))
        replayer = Replayer(
            db,
            state / "data",
            CALENDAR.session,
            windows,
            {p.cell: replay.Source(p.role, tuple(days)) for p in picks},
            rules=Rules(fee_rate=FEE, capital=capital),
            target_version=TARGET_VERSION,
            thresholds=thresholds,
        )
        policy = [Ranked(p.cell, 0.6 if p.threshold is None else p.threshold) for p in picks]
        done = replayer.run([policy], alone=alone, record=trades.append)
    finally:
        db.close()
    return done, trades, replayer


def both(
    state: Path, events: Sequence[Event], picks: Sequence[Pick], capital: int = CAPITAL
) -> tuple[list[dict[str, Any]], list[Trade], Pass]:
    """The paper trader's trades (oldest first) on ``events``, and the replay's on the database it read
    and the same quotes recorded."""
    models(state)
    paper = Day(state, picks=picks, rules=Rules(fee_rate=FEE, capital=capital)).run(events)
    record_quotes(state / "data", [e for e in events if isinstance(e, BestQuote)])
    done, trades, _ = replayed(state, picks, capital=capital)
    return list(reversed(paper)), trades, done


def as_paper(trades: Sequence[Trade]) -> list[tuple[Any, ...]]:
    return [
        (t.cell, t.entry_at, t.entry, t.quantity, t.exit_at, t.exit, t.reason, pytest.approx(t.net))
        for t in trades
    ]


def of_paper(trades: Sequence[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            Cell(t["kind"], t["predictor"]),
            datetime.fromisoformat(t["entry"]["at"]),
            t["entry"]["price"],
            t["entry"]["quantity"],
            datetime.fromisoformat(t["exit"]["at"]),
            t["exit"]["price"],
            t["exit"]["reason"],
            t["net"],
        )
        for t in trades
    ]


# The same trades as the paper trader.

# One cell's day: a take-profit, a rise held off, a stop on the best bid, a time exit on the quote before
# the barrier, a target reached already, a time exit on the quote after it. Each signal alone, a share;
# as a policy, by the trader's rules (#471): nine shares a buy, and three net losses in a row by 10:24.
ONE_CELL: list[Event] = [
    quote("10:00:01", 69_900, 70_000),  # before the decision: not the buy's
    Read("10:00:02", [Barrier("10:00:00")]),
    quote("10:00:02.5", 70_000, 70_100),  # bought at 70,100
    quote("10:01:00", 70_200, 70_300),
    Read("10:01:02", [Barrier("10:01:00")]),  # held off: one position at a time
    quote("10:01:02.5", 70_100, 70_200),
    quote("10:02:00", 70_300, 70_400),  # the take-profit at 70,250
    Read("10:10:02", [Barrier("10:10:00")]),
    quote("10:10:02.4", 70_000, 70_100),
    quote("10:11:00", 69_900, 70_000),
    quote("10:11:30", 69_800, 69_900),  # the bid at or below the stop, the ask not: stopped at 69,800
    Read("10:20:02", [Barrier("10:20:00")]),
    quote("10:20:02.3", 70_000, 70_100),
    quote("10:22:00", 70_100, 70_200),
    quote("10:23:59.5", 70_000, 70_100),  # the book standing at 10:24: sold at its bid
    quote("10:24:00.2", 70_300, 70_400),
    Read("10:30:02", [Barrier("10:30:00")]),
    quote("10:30:02.2", 70_200, 70_300),  # its ask at the target already: nothing bought
    Read("10:31:02", [Barrier("10:31:00")]),
    quote("10:31:02.5", 70_000, 70_100),  # the last quote before the 10:35 barrier: too old to sell on
    quote("10:35:01", 70_000, 70_100),  # sold on the quote after the barrier
    Read("10:50:02", [Barrier("10:50:00")]),
    quote("10:50:02.5", 70_000, 70_100),
    quote("10:51:00", 69_700, 69_800),  # stopped at 69,700
    Read("11:00:02", [Barrier("11:00:00")]),
    quote("11:00:02.5", 70_000, 70_100),
    quote("11:01:00", 70_300, 70_400),
]
# The nets of ONE_CELL's eight rises alone, a share: (bought, sold).
ALONE = [
    (70_100, 70_250),  # 10:00
    (70_200, 70_250),  # 10:01
    (70_100, 69_800),  # 10:10
    (70_100, 70_000),  # 10:20
    (70_100, 70_000),  # 10:31
    (70_100, 69_700),  # 10:50
    (70_100, 70_250),  # 11:00
]


def test_the_replay_makes_the_paper_trader_s_trades_on_one_cell(tmp_path: Path) -> None:
    """The paper trader on its quotes and the replay on them recorded: the same entries (time, price,
    shares), exits (time, price, why) and nets. Each buy nine shares (#471: 5,000 won ÷ 510.6925 a
    share at its stop). The 10:00 take-profit nets −103.97 won (bought at the ask), and with the 10:10
    stop and the 10:20 time exit it is the third net loss in a row at 10:24: the 10:30, 10:31 and 10:50
    rises are cooling down, and the 11:00 one, past 10:54, buys."""
    paper, trades, done = both(tmp_path, ONE_CELL, [pick(BARRIER)])

    assert of_paper(paper) == as_paper(trades)
    assert [(t.entry_at, t.entry, t.quantity, t.reason, t.exit_at, t.exit) for t in trades] == [
        (at("10:00:02.5"), 70_100, 9, TAKE_PROFIT, at("10:02:00"), 70_250),
        (at("10:10:02.4"), 70_100, 9, STOP_LOSS, at("10:11:30"), 69_800),
        (at("10:20:02.3"), 70_100, 9, TIME, at("10:24:00"), 70_000),
        (at("11:00:02.5"), 70_100, 9, TAKE_PROFIT, at("11:01:00"), 70_250),
    ]
    assert trades[0].net == pytest.approx(9 * 150 - 9 * 70_100 * FEE - 9 * 70_250 * (FEE + 0.002))
    [result] = done.results
    assert result.total.trades == 4 and result.total.net == pytest.approx(sum(t["net"] for t in paper))
    assert result.skipped == {HOLDING: 1, COOLING_DOWN: 3}
    assert result.days == {TODAY: pytest.approx(result.total.net)}


def test_the_daily_limit_sells_and_stops_the_replay_as_it_does_the_paper_trader(tmp_path: Path) -> None:
    """Bought nine at 70,100, the bid gaps to 59,500 under the stop: stopped there, −96,645.96 won. Nine
    bought again at 10:10; a bid of 69,860, above the stop, opens −3,606.43: the day's −100,252.39
    reaches the daily limit (100,000 won), and the position is sold at that bid at once. The 10:20 rise
    buys nothing (``daily_limit``); the next day trades again (#471: the replay holds the limits as a
    challenger's book simulates them)."""
    events: list[Event] = [
        Read("10:00:02", [Barrier("10:00:00")]),
        quote("10:00:02.5", 70_000, 70_100),
        quote("10:01:00", 59_500, 59_600),
        Read("10:10:02", [Barrier("10:10:00")]),
        quote("10:10:02.5", 70_000, 70_100),
        quote("10:11:00", 69_860, 69_960),
        Read("10:20:02", [Barrier("10:20:00")]),
        quote("10:20:02.5", 70_000, 70_100),
        Read("2026-10-15 10:00:02", [Barrier("2026-10-15 10:00:00")]),
        quote("2026-10-15 10:00:02.5", 70_000, 70_100),
        quote("2026-10-15 10:01:00", 70_300, 70_400),
    ]
    models(tmp_path)
    paper = list(reversed(Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(events)))
    record_quotes(tmp_path / "data", [e for e in events if isinstance(e, BestQuote)])

    done, trades, _ = replayed(tmp_path, [pick(BARRIER)], days=(TODAY, SECOND))

    assert of_paper(paper) == as_paper(trades)
    assert [(t.reason, t.exit) for t in trades] == [
        (STOP_LOSS, 59_500),
        (DAILY_LIMIT, 69_860),
        (TAKE_PROFIT, 70_250),
    ]
    assert sum(t.net for t in trades[:2]) == pytest.approx(-96_645.96 - 3_606.426)
    assert done.results[0].skipped == {DAILY_LIMIT: 1}


def test_the_rank_waits_are_the_paper_trader_s(tmp_path: Path) -> None:
    """breiman ranked first, the 15-minute barrier's baseline second. At 11:00 the second's rise,
    displayed at 11:00:01, waits for the first's prediction, read at 11:00:04 and no rise: it buys at
    the first quote after that (70,200), not after its own display (70,100). At 11:10 the first has
    no prediction: the second buys once its wait is over, 10 s after its display. At 11:20 both rise:
    the first, displayed later, buys, and the second is outranked."""
    events: list[Event] = [
        Read("11:00:01", [Barrier("11:00:00", kind=FIFTEEN.kind, predictor=FIFTEEN.predictor, shown=1.0)]),
        quote("11:00:01.5", 70_000, 70_100),
        Read("11:00:04", [Barrier("11:00:00", p=FALL, shown=4.0)]),
        quote("11:00:04.5", 70_100, 70_200),
        quote("11:01:00", 70_300, 70_400),
        Read("11:10:01", [Barrier("11:10:00", kind=FIFTEEN.kind, predictor=FIFTEEN.predictor, shown=1.0)]),
        quote("11:10:01.5", 70_000, 70_100),
        Tick("11:10:11"),  # its wait over: the trader's clock
        quote("11:10:11.2", 70_000, 70_200),
        quote("11:11:00", 70_300, 70_400),
        Read("11:20:01", [Barrier("11:20:00", kind=FIFTEEN.kind, predictor=FIFTEEN.predictor, shown=1.0)]),
        Read("11:20:03", [Barrier("11:20:00", shown=3.0)]),
        quote("11:20:03.5", 70_000, 70_100),
        quote("11:21:00", 70_300, 70_400),
    ]

    paper, trades, done = both(tmp_path, events, [pick(BARRIER), pick(FIFTEEN, role="baseline")])

    assert of_paper(paper) == as_paper(trades)
    assert [(t.cell, t.decided_at, t.entry_at, t.entry) for t in trades] == [
        (FIFTEEN, at("11:00:04"), at("11:00:04.5"), 70_200),
        (FIFTEEN, at("11:10:11"), at("11:10:11.2"), 70_200),
        (BARRIER, at("11:20:03"), at("11:20:03.5"), 70_100),
    ]
    [result] = done.results
    assert result.skipped == {NOT_UP: 1, OUTRANKED: 1}


def test_a_special_window_counts_from_when_it_was_known(tmp_path: Path) -> None:
    """A VI begun between the 12:00 rise's decision and its first quote gives its buy up, as the paper
    trader gives it up. A VI begun at 12:32, in the 12:30 rise's window but after its buy, changes
    nothing: the trader knew nothing of it when it bought (the answer's special flag is never read)."""
    events: list[Event] = [
        Read("12:00:02", [Barrier("12:00:00")]),
        Read("12:00:02.2", [Special("12:00:02.2", "12:02:00")]),
        quote("12:00:02.5", 70_000, 70_100),
        Read("12:30:02", [Barrier("12:30:00")]),
        quote("12:30:02.5", 70_000, 70_100),
        Read("12:32:00", [Special("12:32:00", "12:33:00")]),
        quote("12:32:30", 70_300, 70_400),
    ]

    paper, trades, done = both(tmp_path, events, [pick(BARRIER)])

    assert of_paper(paper) == as_paper(trades)
    assert [(t.bar_close, t.reason) for t in trades] == [(at("12:30:00"), TAKE_PROFIT)]
    assert done.results[0].skipped == {SPECIAL: 1}


def test_the_direction_is_stopped_on_the_best_bid_as_the_paper_trader_stops_it(tmp_path: Path) -> None:
    """The final consensus (k 2 around 70,050): the stop at 69,850, reached by the bid at 13:01:00. The
    re-fit's replay of the answers' paths had no stop for the direction (its touch P&L)."""
    events: list[Event] = [
        Read("13:00:02", [Direction("13:00:00")]),
        quote("13:00:02.5", 70_000, 70_100),
        quote("13:01:00", 69_800, 69_900),
    ]

    paper, trades, _ = both(tmp_path, events, [pick(DIRECTION, role="consensus")])

    assert of_paper(paper) == as_paper(trades)
    [trade] = trades
    assert (trade.reason, trade.exit) == (STOP_LOSS, 69_800)


# The rules, measured alone a share.


def test_each_signal_alone_buys_on_the_first_quote_after_its_display_and_pays_on_its_fills(
    tmp_path: Path,
) -> None:
    """ONE_CELL's eight rises each alone, a share: none is held off. The 10:00 one bought at 70,100 (the
    first ask after 10:00:02, not 70,000 standing at its bar close) and sold at the target 70,250: 150
    won less the commission on 70,100 and on 70,250 (10.515 + 10.5375) and the tax on 70,250 (140.5):
    −11.5525 won, −0.115525 ticks. The 10:10 one, stopped at the bid 69,800 (its ask 69,900 above the
    stop), loses 300 + 10.515 + 10.47 + 139.6 = 460.585 won. The 10:30 one is not bought."""
    models(tmp_path)
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(ONE_CELL)
    record_quotes(tmp_path / "data", [e for e in ONE_CELL if isinstance(e, BestQuote)])

    done, _, _ = replayed(tmp_path, [pick(BARRIER)], thresholds=(0.6, 0.8), alone=BARRIER)

    up = 70_250 - 70_100 - (70_100 * FEE + 70_250 * FEE + 70_250 * 0.002)
    assert up == pytest.approx(-11.5525) and net_of(70_100, 70_250, 1, FEE) == pytest.approx(up)
    down = 69_800 - 70_100 - (70_100 * FEE + 69_800 * FEE + 69_800 * 0.002)
    assert down == pytest.approx(-460.585) and net_of(70_100, 69_800, 1, FEE) == pytest.approx(down)
    assert done.counts == {"read": 8, "rises": 8}
    alone = done.signals[0.6]
    assert (alone.signals, alone.measured, alone.bought, dict(alone.refused)) == (
        8,
        8,
        7,
        {TARGET_REACHED: 1},
    )
    assert alone.exits == {TAKE_PROFIT: 3, STOP_LOSS: 2, TIME: 2}
    assert alone.net == pytest.approx(sum(net_of(bought, sold, 1, FEE) for bought, sold in ALONE))
    assert alone.ticks == pytest.approx(alone.net / 100), "ticks of the reference: 100 won"
    assert alone.days == {TODAY: [8, pytest.approx(alone.ticks)]}, "the one not bought nets 0"
    assert done.signals[0.8].signals == 0, "every rise at 0.7"


def test_a_rise_read_after_its_time_barrier_s_last_five_seconds_is_not_bought(tmp_path: Path) -> None:
    """A rise decided at 14:03:58, its first quote at 14:04:00.5, at its 14:04 time barrier: expired."""
    events: list[Event] = [
        Read("14:03:58", [Barrier("14:00:00", shown=238.0)]),
        quote("14:04:00.5", 70_000, 70_100),
    ]

    paper, trades, done = both(tmp_path, events, [pick(BARRIER)])

    assert paper == [] and trades == []
    assert done.results[0].skipped == {EXPIRED: 1}


def test_the_clock_sells_on_the_last_quote_before_the_barrier_when_none_comes_after(tmp_path: Path) -> None:
    """No quote after the 14:34 barrier: the clock sells at its bid, 3 s before it, as the paper trader's
    clock does a second past the barrier."""
    events: list[Event] = [
        Read("14:30:02", [Barrier("14:30:00")]),
        quote("14:30:02.5", 70_000, 70_100),
        quote("14:33:57", 70_100, 70_200),
        Tick("14:34:01"),
    ]

    paper, trades, _ = both(tmp_path, events, [pick(BARRIER)])

    assert of_paper(paper) == as_paper(trades)
    [trade] = trades
    assert (trade.exit_at, trade.exit, trade.reason) == (at("14:34:00"), 70_100, TIME)


# Missing recordings: left out and counted, never a trade of no gain.


def test_a_signal_in_a_gap_of_the_recording_is_left_out_and_counted(tmp_path: Path) -> None:
    """The book's feed of 005930 gone from 10:11:10 to 10:11:20, in the 10:10 rise's window: it is left
    out of the policy and of the signals alone (``gap``), the rest replayed as before: the 10:31 trade's
    loss is the third in a row, and the 10:50 and 11:00 rises are cooling down (#471)."""
    models(tmp_path)
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(ONE_CELL)
    record_quotes(tmp_path / "data", [e for e in ONE_CELL if isinstance(e, BestQuote)])
    record_gap(tmp_path / "data", at("10:11:10"), at("10:11:20"))

    done, trades, replayer = replayed(tmp_path, [pick(BARRIER)], alone=BARRIER)

    [result] = done.results
    assert result.missing == {GAP: 1}
    assert [t.bar_close for t in trades] == [at(f"{hm}:00") for hm in ("10:00", "10:20", "10:31")], (
        "the 10:10 rise neither traded nor held anything"
    )
    alone = done.signals[0.6]
    assert (alone.signals, alone.measured, dict(alone.missing)) == (8, 7, {GAP: 1})
    assert replayer.recordings == {TODAY: replay.GAPPED}, "a gap in the session"


def test_no_quote_after_the_decision_or_none_to_sell_on_leaves_the_signal_out(tmp_path: Path) -> None:
    """The 14:00 rise's first quote comes 8 s after its decision: the stream was down (the paper trader
    gives the buy up, ``no_quote``). The 14:10 rise is bought, and its recording ends 10 s before its
    barrier: nothing to sell on (the paper trader would hold it to the next quote). Both are left out,
    neither a trade."""
    models(tmp_path)
    events: list[Event] = [
        Read("14:00:02", [Barrier("14:00:00")]),
        quote("14:00:10", 70_000, 70_100),
        Read("14:10:02", [Barrier("14:10:00")]),
        quote("14:10:02.5", 70_000, 70_100),
        quote("14:13:50", 70_100, 70_200),
    ]
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(events)
    record_quotes(tmp_path / "data", [e for e in events if isinstance(e, BestQuote)])

    done, trades, _ = replayed(tmp_path, [pick(BARRIER)], alone=BARRIER)

    assert trades == []
    assert done.results[0].missing == {NO_QUOTE: 1, NO_EXIT_QUOTE: 1}
    assert done.signals[0.6].missing == {NO_QUOTE: 1, NO_EXIT_QUOTE: 1}


def test_a_day_without_its_recording_is_left_out_and_counted(tmp_path: Path) -> None:
    models(tmp_path)
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(ONE_CELL)

    done, trades, replayer = replayed(tmp_path, [pick(BARRIER)], alone=BARRIER)

    assert trades == []
    assert done.results[0].missing == {NO_RECORDING: 8}, "each rise decided, none traded nor held"
    assert done.signals[0.6].missing == {NO_RECORDING: 8}
    assert done.signals[0.6].measured == 0 and done.signals[0.6].per_signal() is None
    assert replayer.recordings == {TODAY: NO_RECORDING}


def test_an_algorithm_s_prediction_of_another_target_s_model_never_buys(tmp_path: Path) -> None:
    """breiman's model v1 was trained on the barrier's answer; a prediction of a model of an unknown
    target is read (it counts for the cells ranked below) and buys nothing."""
    events: list[Event] = [Read("10:00:02", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)]
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(events)  # no model recorded
    record_quotes(tmp_path / "data", [e for e in events if isinstance(e, BestQuote)])

    done, trades, _ = replayed(tmp_path, [pick(BARRIER)], alone=BARRIER)

    assert trades == []
    assert done.results[0].skipped == {OFF_TARGET: 1}
    assert done.counts == {"read": 1, OFF_TARGET: 1}


# A day at a time.

SECOND = date(2026, 10, 15)


def test_the_recordings_are_read_a_day_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each day's recording is let go before the next is read: two are never held at once."""
    models(tmp_path)
    events: list[Event] = [
        Read("10:00:02", [Barrier("10:00:00")]),
        quote("10:00:02.5", 70_000, 70_100),
        quote("10:01:00", 70_300, 70_400),
        Read("2026-10-15 10:00:02", [Barrier("2026-10-15 10:00:00")]),
        quote("2026-10-15 10:00:02.5", 70_000, 70_100),
        quote("2026-10-15 10:01:00", 70_300, 70_400),
    ]
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(events)
    record_quotes(tmp_path / "data", [e for e in events if isinstance(e, BestQuote)])
    alive: list[weakref.ref[Recording]] = []
    read_recording = replay.read_recording

    def tracked(data_dir: Path, day: date) -> Recording | None:
        assert all(ref() is None for ref in alive), "the day before is let go first"
        found = read_recording(data_dir, day)
        if found is not None:
            alive.append(weakref.ref(found))
        return found

    monkeypatch.setattr(replay, "read_recording", tracked)

    done, trades, replayer = replayed(tmp_path, [pick(BARRIER)], days=(TODAY, SECOND), alone=BARRIER)

    assert len(alive) == 2 and len(trades) == 2
    assert replayer.recordings == {TODAY: replay.RECORDED, SECOND: replay.RECORDED}
    assert set(replayer.peaks) == {TODAY, SECOND}, "each day's peak resident memory"
    assert done.results[0].days.keys() == {TODAY, SECOND}


def test_a_quote_not_newer_than_the_one_before_is_passed_over(tmp_path: Path) -> None:
    """Two quotes received at the same microsecond: the paper trader takes the first and passes the
    second over; the replay too (here the book recorded first)."""
    events: list[Event] = [
        Read("10:00:02", [Barrier("10:00:00")]),
        quote("10:00:02.5", 70_000, 70_100),
        quote("10:00:02.5", 70_100, 70_200),  # passed over
        quote("10:01:00", 70_300, 70_400),
    ]

    paper, trades, _ = both(tmp_path, events, [pick(BARRIER)])

    assert of_paper(paper) == as_paper(trades)
    assert trades[0].entry == 70_100
