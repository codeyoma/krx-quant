"""The cells the trader trades (#395): picked from the cost map, ranked, sized by their Kelly
fraction, and not bought in a special situation.

Input: made-up cost maps within the /v1 schema (``tests/trader_doubles.py``) and the engine's own
cost map; predictions, special windows and quotes as the trader reads them. Output: the cells
picked, the trader's decisions and its trades.

The two cells ranked throughout: the 4-minute barrier's ``breiman`` (lower bound 0.70, p* 0.62:
0.08) above the 15-minute barrier's ``barrier-persist-15m`` (0.66, 0.60: 0.06).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import aiohttp
import pytest

from krx_quant.special import Kind as SpecialKind
from krx_quant.trader import KELLY_FRACTION
from krx_quant.trader.cells import Cell
from krx_quant.trader.paper import (
    BLOCKED,
    HOLDING,
    NO_SHARES,
    NOT_UP,
    OUTRANKED,
    SPECIAL,
    Rules,
)
from krx_quant.trader.selection import NO_PASS, CostMapError, choose, kelly, read_cost_map
from krx_quant.trader.store import DB_NAME, TraderStore, read_trades
from tests.api_schema import check
from tests.test_cost_map_api import barrier_board
from tests.test_cost_map_api import cost_map as engine_cost_map
from tests.trader_doubles import (
    BARRIER,
    DIRECTION,
    FEE,
    Barrier,
    Day,
    Direction,
    Read,
    Special,
    Tick,
    at,
    cost_map,
    cost_row,
    pick,
    predictor,
    quote,
)

FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
FIRST = pick(BARRIER, low=0.70, p_star=0.62)
SECOND = pick(FIFTEEN, role="baseline", low=0.66, p_star=0.60)
RANKED = (FIRST, SECOND)


def rise(
    close: str, cell: Cell = BARRIER, shown: float = 2.0, p: tuple[float, float, float] = (0.7, 0.2, 0.1)
) -> Barrier:
    return Barrier(close, kind=cell.kind, predictor=cell.predictor, shown=shown, p=p)


# Picking from the cost map.


def test_only_the_cells_passing_at_a_market_order_s_round_trip_are_picked() -> None:
    """W 10 and C 2.6 ticks: p* = 12.6 ÷ 20 = 0.63 at the market (0.58 at a limit order)."""
    body = cost_map(
        [
            cost_row(
                "direction",
                consensus=predictor("consensus-final", 0.70, "few", calls=120, rank=None),
                top=predictor("alpha", 0.60, "fail"),
                baseline=predictor("baseline-persist", 0.60, {"limit": "pass", "market": "fail"}),
            ),
            cost_row(
                "barrier",
                top=predictor("breiman", 0.70, "pass"),
                baseline=predictor("barrier-climate", 0.5, "fail"),
            ),
            cost_row(
                "barrier_15m",
                top=predictor("breiman-15m", 0.70, "few"),
                baseline=predictor(FIFTEEN.predictor, 0.66, "pass"),
            ),
            cost_row("barrier_30m", top=predictor("breiman-30m", 0.62, {"limit": "pass", "market": "fail"})),
            cost_row("barrier_60m", top=predictor("breiman-60m", 0.90, "pass")),  # not a traded kind
            cost_row("open_to_close", top=predictor("cox", 0.90, None)),  # no p*, and not traded
        ]
    )
    check("cost_map", body)

    chosen = choose(body)

    assert [(p.kind, p.role, p.predictor) for p in chosen.picks] == [
        ("barrier", "top", "breiman"),
        ("barrier_15m", "baseline", FIFTEEN.predictor),
    ], "the greater lower bound less p* first: 0.07, then 0.03"
    assert [p.priority for p in chosen.picks] == pytest.approx([0.07, 0.03])
    assert chosen.reason is None and chosen.as_of == "2026-10-14T08:30:00+09:00"
    seen = {(c["kind"], c["role"]): (c["predictor"], c["verdict"], c["picked"]) for c in chosen.considered}
    assert seen == {
        ("direction", "consensus"): ("consensus-final", "few", False),
        ("direction", "top"): ("alpha", "fail", False),
        ("direction", "baseline"): ("baseline-persist", "fail", False),
        ("barrier", "top"): ("breiman", "pass", True),
        ("barrier", "baseline"): ("barrier-climate", "fail", False),
        ("barrier_15m", "top"): ("breiman-15m", "few", False),
        ("barrier_15m", "baseline"): (FIFTEEN.predictor, "pass", True),
        ("barrier_30m", "top"): ("breiman-30m", "fail", False),
        ("barrier_30m", "baseline"): (None, None, False),
    }, "the kinds traded only, the consensus the direction's only"


def test_the_kelly_fraction_worked_by_hand() -> None:
    """W 10 and C 2 ticks at 100,000 won (tick 100): a call right gains 8 ticks, g = 0.008 of the
    price; one wrong loses 12 ticks, ℓ = 0.012. p* = 12 ÷ 20 = 0.6. At a lower bound of 0.6024,
    f* = 0.6024 ÷ 0.012 − 0.3976 ÷ 0.008 = 50.2 − 49.7 = 0.5: half of it, 0.25 of the capital."""
    assert kelly(0.6024, 10, 2, 100, 100_000) == pytest.approx(0.5)
    assert kelly(0.6, 10, 2, 100, 100_000) == pytest.approx(0.0), "at p* exactly nothing"
    assert kelly(0.9, 2, 2.6, 100, 70_050) is None, "W ≤ C: a call right does not pay its cost"
    body = cost_map(
        [
            cost_row("barrier", width=10, cost=2, price=100_000, top=predictor("breiman", 0.6024, "pass")),
            cost_row("barrier_15m", width=10, cost=2, price=100_000, top=predictor("b15", 0.70, "pass")),
        ]
    )

    tight, wide = sorted(choose(body).picks, key=lambda p: p.low)

    assert (tight.kelly, tight.fraction) == pytest.approx((0.5, KELLY_FRACTION * 0.5))
    # 0.70 ÷ 0.012 − 0.30 ÷ 0.008 = 58.33 − 37.5 = 20.8: far above 1, the whole capital.
    assert (wide.kelly, wide.fraction) == pytest.approx((20.8333, 1.0), abs=1e-3)


def test_a_pass_whose_kelly_is_not_above_zero_is_not_picked() -> None:
    """The verdict is the engine's; the trader sizes the cells it trades on the same figures."""
    body = cost_map(
        [cost_row("barrier", width=10, cost=2, price=100_000, top=predictor("breiman", 0.6, "pass"))]
    )

    chosen = choose(body)

    assert chosen.picks == () and chosen.reason == NO_PASS
    [found] = [c for c in chosen.considered if c["role"] == "top" and c["kind"] == "barrier"]
    assert (found["verdict"], found["picked"], found["why"]) == ("pass", False, "Kelly at most 0")


def test_a_cost_map_of_another_symbol_or_period_is_not_read() -> None:
    with pytest.raises(CostMapError, match="not 005930's of 20d"):
        choose(cost_map([], symbol="all"))
    with pytest.raises(CostMapError, match="not 005930's of 20d"):
        choose(cost_map([], period="today"))
    with pytest.raises(CostMapError, match="no rows"):
        choose({"error": "symbol must be all or one of 005930"})


async def test_the_engine_s_own_cost_map_is_read_and_picked_from(tmp_path: Path) -> None:
    """The Engine API's answer, as the trader asks for it: the 10-tick barrier at 81,250 won where
    alpha calls every rise and fall right (lower bound 0.84 above p* 0.64) passes; the always-up
    baseline fails; the direction, without predictions, has no predictor."""
    async with engine_cost_map(tmp_path) as (engine, client):
        barrier_board(engine.store)
        async with aiohttp.ClientSession() as session:
            body = await read_cost_map(session, str(client.make_url("")).rstrip("/"))

    check("cost_map", body)
    chosen = choose(body)

    [only] = chosen.picks
    assert (only.kind, only.role, only.predictor) == ("barrier", "top", "alpha")
    assert only.p_star == pytest.approx((10 + 2.86875) / 20)
    assert (only.width, only.reference_price, only.tick) == (10, 81_250, 100)
    assert only.fraction == 1.0
    verdicts = {(c["kind"], c["role"]): c["verdict"] for c in chosen.considered}
    assert verdicts[("barrier", "baseline")] == "fail"
    assert verdicts[("direction", "top")] is None


# No cell passing.


def test_a_day_without_a_cell_passing_trades_nothing_and_keeps_why(tmp_path: Path) -> None:
    body = cost_map(
        [cost_row("barrier", top=predictor("breiman", 0.60, "fail")), cost_row("direction")],
    )
    day = Day(tmp_path, selection=choose(body))

    trades = day.run([Read("10:00:02.3", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)])

    assert trades == [] and day.decisions() == [], "no cell is read"
    [kept] = day.selections()
    assert (kept["reason"], kept["picks"], kept["as_of"]) == (NO_PASS, "[]", "2026-10-14T08:30:00+09:00")
    assert '"verdict": "fail"' in kept["considered"]


# The rank among the rises of one bar close.


def test_of_two_rises_read_together_the_first_ranked_buys(tmp_path: Path) -> None:
    day = Day(tmp_path, picks=RANKED)

    [trade] = day.run(
        [
            Read("10:00:02.3", [rise("10:00:00", FIFTEEN), rise("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
        ]
    )

    assert (trade["kind"], trade["predictor"]) == ("barrier", "breiman")
    assert {(d["kind"], d["action"], d["reason"]) for d in day.decisions()} == {
        ("barrier", "buy", None),
        ("barrier_15m", "skip", OUTRANKED),
    }


def test_a_lower_ranked_rise_waits_for_the_first_ranked_cell_of_its_bar_close(tmp_path: Path) -> None:
    """The baseline's rise comes first (10:00:02); the algorithm ranked above it at 10:00:05, within
    the ten seconds the baseline's waits: the algorithm's buys, decided when it was read."""
    day = Day(tmp_path, picks=RANKED)

    [trade] = day.run(
        [
            Read("10:00:02.3", [rise("10:00:00", FIFTEEN)]),
            quote("10:00:02.5", 70_000, 70_100),  # nothing decided yet: not a buy's
            Read("10:00:05.3", [rise("10:00:00", shown=5.0)]),
            quote("10:00:05.5", 70_000, 70_100),
        ]
    )

    assert (trade["kind"], trade["decided_at"]) == ("barrier", "2026-10-14T10:00:05.300000+09:00")
    assert trade["entry"]["at"] == "2026-10-14T10:00:05.500000+09:00"
    assert [(d["kind"], d["reason"], d["decided_at"][11:21]) for d in day.decisions()] == [
        ("barrier", None, "10:00:05.3"),
        ("barrier_15m", OUTRANKED, "10:00:05.3"),
    ]


def test_without_the_first_ranked_cell_s_prediction_the_wait_ends_ten_seconds_after_the_display(
    tmp_path: Path,
) -> None:
    """Displayed at 10:00:02, the baseline's rise is decided at 10:00:12 (``Rules.fresh``): it buys
    on the first quote after. The algorithm's, displayed at 10:00:12.5, finds the position taken."""
    day = Day(tmp_path, picks=RANKED)

    [trade] = day.run(
        [
            Read("10:00:02.3", [rise("10:00:00", FIFTEEN)]),
            Tick("10:00:11.9"),
            quote("10:00:11.95", 70_000, 70_100),  # still waiting
            Tick("10:00:12"),
            quote("10:00:12.5", 70_000, 70_100),
            Read("10:00:13", [rise("10:00:00", shown=12.5)]),
        ]
    )

    assert (trade["kind"], trade["decided_at"]) == ("barrier_15m", "2026-10-14T10:00:12+09:00")
    assert trade["entry"]["at"] == "2026-10-14T10:00:12.500000+09:00"
    assert [(d["kind"], d["reason"]) for d in day.decisions()] == [
        ("barrier_15m", None),
        ("barrier", HOLDING),
    ]


def test_a_first_ranked_cell_without_a_rise_lets_the_next_buy_at_once(tmp_path: Path) -> None:
    day = Day(tmp_path, picks=RANKED)

    [trade] = day.run(
        [
            Read("10:00:02.3", [rise("10:00:00", FIFTEEN)]),
            Read("10:00:04.3", [rise("10:00:00", shown=4.0, p=(0.1, 0.2, 0.7))]),  # down first
            quote("10:00:04.5", 70_000, 70_100),
        ]
    )

    assert (trade["kind"], trade["decided_at"]) == ("barrier_15m", "2026-10-14T10:00:04.300000+09:00")
    assert [(d["kind"], d["reason"]) for d in day.decisions()] == [("barrier", NOT_UP), ("barrier_15m", None)]


# The size.


@pytest.mark.parametrize(
    ("fraction", "capital", "quantity"),
    [
        (1.0, 1_000_000, 14),  # a Kelly above 1: the whole capital, ⌊1,000,000 ÷ 70,100⌋
        (0.25, 1_000_000, 3),  # ⌊250,000 ÷ 70,100⌋ = ⌊3.57⌋
        (0.5, 100_000, None),  # 50,000 won does not buy a share at 70,100: nothing
    ],
)
def test_a_buy_takes_its_cell_s_fraction_of_the_capital_in_whole_shares(
    tmp_path: Path, fraction: float, capital: int, quantity: int | None
) -> None:
    day = Day(tmp_path, picks=[pick(BARRIER, fraction=fraction)], rules=Rules(fee_rate=FEE, capital=capital))

    trades = day.run([Read("10:00:02.3", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)])

    if quantity is None:
        assert trades == []
        assert [d["reason"] for d in day.decisions()] == [NO_SHARES]
    else:
        assert [t["entry"]["quantity"] for t in trades] == [quantity]


def test_a_trade_keeps_its_cell_s_pick(tmp_path: Path) -> None:
    [trade] = Day(tmp_path, picks=[pick(BARRIER, low=0.70, p_star=0.62, fraction=0.25)]).run(
        [Read("10:00:02.3", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)]
    )

    f = kelly(0.70, 10, 2.6, 100, 70_050)
    assert (trade["role"], trade["p_star"], trade["precision_low"]) == ("top", 0.62, 0.70)
    assert (trade["kelly"], trade["fraction"]) == (pytest.approx(f), 0.25)


# Special situations.


@pytest.mark.parametrize(
    ("shown", "reason"),
    [
        (Special("10:01:00", "10:03:30"), SPECIAL),  # a VI of 005930 within the four minutes
        (Special("09:59:00", "10:01:30"), SPECIAL),  # one on at the bar close
        (Special("09:50:00", "09:52:30"), None),  # one over before it
        (Special("10:01:00", "10:03:30", symbol="000660"), None),  # another symbol's
        (Special("09:58:00", "10:28:00", SpecialKind.CIRCUIT_BREAKER), SPECIAL),  # the whole market's
        (Special("10:00:01", "10:05:01", SpecialKind.SIDECAR), SPECIAL),
    ],
)
def test_no_buy_while_a_special_situation_touches_the_window(
    tmp_path: Path, shown: Special, reason: str | None
) -> None:
    """The 10:00 barrier's window runs to 10:04: a special window the engine learned by the time
    the prediction is read and that meets it stops the buy."""
    day = Day(tmp_path)
    day.run(
        [
            Read("09:50:00", [shown]),
            Read("10:00:02.3", [Barrier("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
        ]
    )

    assert [d["reason"] for d in day.decisions()] == [reason]


def test_a_rise_waiting_for_the_cells_above_it_is_not_bought_once_a_special_window_touches_it(
    tmp_path: Path,
) -> None:
    """The 15-minute baseline's rise waits from 10:00:02.3; a VI of 005930 learned at 10:00:06
    touches its window (to 10:15): it is special when its wait ends at 10:00:12."""
    day = Day(tmp_path, picks=RANKED)

    trades = day.run(
        [
            Read("10:00:02.3", [rise("10:00:00", FIFTEEN)]),
            Read("10:00:06", [Special("10:00:06", "10:02:36")]),
            Tick("10:00:12"),
            quote("10:00:12.5", 70_000, 70_100),
        ]
    )

    assert trades == []
    assert [(d["kind"], d["reason"], d["decided_at"][11:19]) for d in day.decisions()] == [
        ("barrier_15m", SPECIAL, "10:00:12")
    ]


def test_a_buy_waiting_for_its_quote_is_given_up_once_a_special_window_touches_it(tmp_path: Path) -> None:
    """Decided at 10:00:02.3; a VI of 005930 learned at 10:00:03, before the first quote (10:00:03.5):
    no buy (Codex review on #412)."""
    day = Day(tmp_path)

    trades = day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            Read("10:00:03", [Special("10:00:03", "10:05:33")]),
            quote("10:00:03.5", 70_000, 70_100),
        ]
    )

    assert trades == []
    assert [(d["action"], d["reason"]) for d in day.decisions()] == [("buy", SPECIAL)]


def test_the_engine_s_own_special_situation_calls_stop_the_buy(tmp_path: Path) -> None:
    """No prediction made for a VI (status vi) and a final consensus the engine blocked for a VI or
    a circuit breaker (#88) are special; one blocked for want of a reference is blocked."""
    barrier = Day(tmp_path / "vi")
    barrier.run([Read("10:00:02.3", [Barrier("10:00:00", status="vi")])])
    consensus = Day(tmp_path / "blocked", cell=DIRECTION)
    consensus.run(
        [
            Read("10:00:02.3", [Direction("10:00:00", blocked="special")]),
            Read("10:01:02.3", [Direction("10:01:00", blocked="no_reference")]),
        ]
    )

    assert [d["reason"] for d in barrier.decisions()] == [SPECIAL]
    assert [d["reason"] for d in consensus.decisions()] == [SPECIAL, BLOCKED]


# The store.


def test_a_store_made_before_the_picks_gets_their_columns(tmp_path: Path) -> None:
    """A store #394's trader made, with a trade in it: opened, it gains the columns, null in that row."""
    path = tmp_path / DB_NAME
    with closing(sqlite3.connect(path)) as db:
        db.executescript(OLD_SCHEMA)
        times = [at(t).isoformat() for t in ("10:00:00", "10:04:00", "10:00:02.3", "10:00:02.5")]
        db.execute(
            "INSERT INTO trades (mode, symbol, kind, predictor, prediction_id, bar_close, probability,"
            " threshold, anchor, tick, width_ticks, target, stop, time_barrier, decided_at, entry_at,"
            " entry_price, quantity, buy_fee) VALUES ('paper', '005930', 'barrier', 'breiman', 1, ?, 0.7,"
            " 0.6, 70050, 100, 2, 70250, 69850, ?, ?, ?, 70100, 14, 147.21)",
            times,
        )
        db.commit()

    TraderStore(path).close()

    [trade] = read_trades(path, None, 10)
    assert (trade["role"], trade["p_star"], trade["precision_low"], trade["kelly"], trade["fraction"]) == (
        None,
    ) * 5
    with closing(sqlite3.connect(path)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
    assert "selection_id" in columns


# The decisions and trades tables as #394 made them (before #395).
OLD_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
    predictor TEXT NOT NULL, prediction_id INTEGER NOT NULL, bar_close TEXT NOT NULL,
    displayed_at TEXT NOT NULL, decided_at TEXT NOT NULL, probability REAL, action TEXT NOT NULL,
    reason TEXT, trade_id INTEGER REFERENCES trades (id), UNIQUE (mode, kind, predictor, prediction_id)
);
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
    predictor TEXT NOT NULL, prediction_id INTEGER NOT NULL, bar_close TEXT NOT NULL,
    probability REAL NOT NULL, threshold REAL NOT NULL, anchor REAL NOT NULL, tick REAL NOT NULL,
    width_ticks INTEGER NOT NULL, target REAL NOT NULL, stop REAL NOT NULL, time_barrier TEXT NOT NULL,
    decided_at TEXT NOT NULL, entry_at TEXT NOT NULL, entry_price REAL NOT NULL, quantity INTEGER NOT NULL,
    buy_fee REAL NOT NULL, exit_at TEXT, exit_price REAL, exit_reason TEXT, sell_fee REAL, tax REAL,
    cost REAL, gross REAL, net REAL
);
"""
