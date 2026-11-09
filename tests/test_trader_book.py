"""The book's rules (#652, ``trader/book.py``), which the paper book, the KIS book and the replay all ask:
why a rise buys nothing now, and what an exit and a loss limit judge, of one quote and of a day's array
of them alike. The fills each book makes on its own quotes are its adapter's: the paper book's
(``tests/test_trader_paper.py``), the KIS book's (``tests/test_trader_mock.py``) and the replay's
recorded arrays (``tests/test_trader_replay.py``)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from krx_quant.trader.book import (
    COOLING_DOWN,
    HOLDING,
    OUTRANKED,
    SPACING,
    STALLED,
    Busy,
    Taken,
    not_now,
    stopped,
    takes_profit,
)
from krx_quant.trader.cells import Cell, Signal
from krx_quant.trader.limits import RUNNING, reaches
from krx_quant.trader.mock import RECONCILING
from krx_quant.trader.store import DAILY_LIMIT
from tests.trader_doubles import BARRIER, at

FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
RANKS = {BARRIER: 0, FIFTEEN: 1}


def rise(cell: Cell, close: str) -> Signal:
    bar_close = at(close)
    return Signal(
        1, cell.kind, cell.predictor, "005930", bar_close, bar_close + timedelta(seconds=2), "ok",
        (0.7, 0.2, 0.1), 0, 70_050.0, 100.0, 2, timedelta(minutes=4),
    )  # fmt: skip


def asked(
    signal: Signal,
    busy: Busy = None,
    *,
    status: str = RUNNING,
    stalled: bool = False,
    risk: str | None = None,
    spacing: str | None = None,
    live: bool = True,
) -> tuple[str | None, list[str]]:
    """``not_now`` of ``signal`` at 10:00:03, and which of the store's questions it asked (the risk rules,
    the spacing): the paper book reads both from its store, so they are asked last."""
    reads: list[str] = []

    def risk_of(when: datetime) -> str | None:
        reads.append("risk")
        return risk

    def spacing_of(signal: Signal, when: datetime) -> str | None:
        reads.append("spacing")
        return spacing

    found = not_now(
        signal,
        at("10:00:03"),
        status=status,
        busy=busy,
        rank=RANKS.__getitem__,
        risk=risk_of,
        stalled=stalled,
        spacing=spacing_of if live else None,
    )
    return found, reads


SECOND = rise(FIFTEEN, "10:00:00")
FIRST_SAME_CLOSE = Taken(BARRIER, at("10:00:00"))
FIRST_EARLIER = Taken(BARRIER, at("09:56:00"))


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        ("free", {}, (None, ["risk", "spacing"])),
        ("a limit reached", {"status": DAILY_LIMIT, "busy": FIRST_SAME_CLOSE}, (DAILY_LIMIT, [])),
        ("stalled before held", {"stalled": True, "busy": FIRST_SAME_CLOSE}, (STALLED, [])),
        ("outranked: same bar close, ranked above", {"busy": FIRST_SAME_CLOSE}, (OUTRANKED, [])),
        ("holding: an earlier bar close", {"busy": FIRST_EARLIER}, (HOLDING, [])),
        ("the book's own reason", {"busy": RECONCILING}, (RECONCILING, [])),
        ("an order in flight of no signal", {"busy": HOLDING}, (HOLDING, [])),
        ("risk before spacing", {"risk": COOLING_DOWN, "spacing": SPACING}, (COOLING_DOWN, ["risk"])),
        ("the spacing last", {"spacing": SPACING}, (SPACING, ["risk", "spacing"])),
        ("the replay: no spacing", {"spacing": SPACING, "live": False}, (None, ["risk"])),
    ],
)  # fmt: skip
def test_why_a_rise_buys_nothing_now(case: str, kwargs: dict[str, object], expected: object) -> None:
    assert asked(SECOND, **kwargs) == expected, case  # type: ignore[arg-type]


def test_a_cell_ranked_below_is_held_not_outranked() -> None:
    """The first ranked rise of a bar close the second holds is ``holding``: it was not outranked."""
    assert asked(rise(BARRIER, "10:00:00"), Taken(FIFTEEN, at("10:00:00")))[0] == HOLDING


def test_the_exits_and_the_limits_judge_a_bid_and_a_day_s_array_alike() -> None:
    """The replay searches a day's arrays with the very predicates the paper book asks of one quote."""
    bids = np.array([70_250.0, 70_249.0, 69_850.0, 69_851.0, -100_000.0])

    assert list(takes_profit(bids, 70_250)) == [takes_profit(float(b), 70_250) for b in bids]
    assert list(stopped(bids, 69_850)) == [stopped(float(b), 69_850) for b in bids]
    assert list(reaches(bids - 70_000, 100_000)) == [reaches(float(b) - 70_000, 100_000) for b in bids]
    assert [bool(x) for x in reaches(np.array([-99_999.0, -100_000.0]), 100_000)] == [False, True]
