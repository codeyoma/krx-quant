"""The live engine lets go of the session's memory once it is over (#623, ``garbage.py``).

Input: two trading days of books, trades and flows through two engines, one that rests as the
live engine does (``Garbage``) and one that never does, with an algorithm that reads every feature;
a Market View with a window still to be answered; a book's updates and running totals forgotten up
to a time in their middle. Output: what each engine's algorithm read and what each stored are the
same on both days, the next morning's too; the rest's two lines of the first day; nothing let go
while a window waits; a log that forgot answers the same from that time on.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.clock import FakeClock
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import FEATURES, Leaders
from krx_quant.garbage import REST_RUN, Garbage
from krx_quant.kind_registry import KINDS
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_view import LET_GO, MarketView, _Book, _Running
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_doubles import stored
from tests.replay_events import (
    DAY,
    FEE,
    SYMBOL,
    at,
    book,
    estimates,
    foreign_members,
    program,
    trade,
    with_sides,
)

NEXT_DAY = DAY + timedelta(days=1)
TWO_DAYS = MarketCalendar({DAY: True, NEXT_DAY: True})
LINE = re.compile(
    r"memory handed back after (the session|the review): let go of (.+); kept (.+);"
    r" engine rss (\d+|\?) -> (\d+|\?) MB, swapped (\d+|\?) MB;"
    r" python \d+ blocks, malloc (\d+|\?) MB in use and (\d+|\?) MB free, arrow (\d+|\?) MB; \d+\.\d\ds"
)


@pytest.fixture(autouse=True)
def unfrozen() -> Iterator[None]:
    """The other tests run with nothing frozen, whatever a test here left."""
    yield
    gc.unfreeze()


def wandering(start: datetime, end: datetime) -> list[Event]:
    """A book with sizes, trades and flows that wander from ``start`` to ``end``: every 13 s a book
    update and a trade, every 23 s the day's program trading and foreign member firms so far."""
    events: list[Event] = []
    when, mid, step = start, 81_250, 0
    while when <= end:
        step += 1
        mid += 100 * ((step * 7919) % 5 - 2)
        sizes = (step * 37) % 900 + 100, (step * 53) % 900 + 100
        events.append(
            book(when, mid - 50, mid + 50, bid_qty=sizes[0], ask_qty=sizes[1], deeper=(sizes[1], sizes[0]))
        )
        events.append(trade(when + timedelta(seconds=1), mid + 50, step % 9 + 1, bid=mid - 50, ask=mid + 50))
        when += timedelta(seconds=13)
    when, net, members, step = start + timedelta(seconds=30), 0, 0, 0
    while when <= end:
        step += 1
        net += 50_000_000 * ((step * 31) % 7 - 3)
        members += 200 * ((step * 17) % 5 - 2)
        events += [program(when, net), foreign_members(when + timedelta(seconds=1), members)]
        when += timedelta(seconds=23)
    return events


def two_days() -> list[Event]:
    """The first day's afternoon to its close, its after-market and an estimate; the next day's
    pre-market and its first hour."""
    events = [
        *wandering(at("13:55:00"), at("15:19:58")),
        *estimates(at("14:31:00"), {"1": (12_000, -3_000)}),
        *wandering(at("15:40:05"), at("16:20:00")),  # the after-market: no bar close reads it
        *wandering(at("08:30:00", NEXT_DAY), at("08:49:50", NEXT_DAY)),  # the pre-market
        *wandering(at("09:00:00", NEXT_DAY), at("10:06:00", NEXT_DAY)),
        *estimates(at("09:31:00", NEXT_DAY), {"1": (7_000, 2_000)}),
    ]
    return with_sides(sorted(events, key=lambda e: e[1]["received_at"]))


async def replayed(db: Path, garbage: Garbage | None) -> tuple[PredictionEngine, FixedAlgorithm]:
    """The two days through an engine of every kind and an algorithm that reads every feature."""
    reader = FixedAlgorithm("reader", features=tuple(sorted(FEATURES)), unique=("velocity",))
    clock = FakeClock(at("13:55:00"))
    engine = PredictionEngine(
        PredictionStore(db),
        TWO_DAYS,
        (SYMBOL,),
        FEE,
        clock,
        algorithms=[reader],
        kind_algorithms=[],
        kinds=KINDS,
        target_version=TARGET_VERSION,
        garbage=garbage,
    )
    await replay_into(engine, clock, two_days(), until=at("10:06:00", NEXT_DAY))
    return engine, reader


def kinds_stored(db: Path) -> dict[str, list[tuple[Any, ...]]]:
    """The touch labels and every kind's predictions and answers, without row IDs."""
    with sqlite3.connect(db) as conn:
        return {
            table: conn.execute(f"SELECT {columns} FROM {table} ORDER BY {columns}").fetchall()
            for table, columns in (
                ("touches", "symbol, bar_close, target_version, class, unscorable, path, scored_at"),
                (
                    "kind_predictions",
                    "kind, symbol, algorithm, bar_close, model_version, displayed_at, status, output,"
                    " flat_ticks, width_ticks",
                ),
                (
                    "kind_answers",
                    "kind, symbol, bar_close, answer_version, class, unscorable, special, path, scored_at",
                ),
            )
        }


# Two engines through two days of every kind's bar closes.
@pytest.mark.timeout(120)
async def test_the_next_morning_is_the_same_after_the_session_s_memory_is_let_go(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: the two days through an engine that rests (after the first day's last answers at
    15:20 and after its review at 15:40) and through one that never does. Output: the features the
    algorithm read at each bar close, and every prediction, score, touch label and kind answer
    stored, are the same on both days; the resting engine said its two lines, the first with the
    first day's book updates, kinds' quotes, trades and flows it let go of, and kept both rests'
    figures in its store's runs."""
    with caplog.at_level(logging.INFO, logger="krx_quant.garbage"):
        resting, read_resting = await replayed(tmp_path / "resting.db", Garbage())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("memory handed back")]
    holding, read_holding = await replayed(tmp_path / "holding.db", None)

    next_morning = sum(1 for p in stored(tmp_path / "holding.db") if p[3].startswith(NEXT_DAY.isoformat()))
    assert next_morning > 60 * 3, "the next day's first hour was predicted and scored"
    assert len(read_holding.seen) > 140, "both days' bar closes read the features"
    assert repr(read_resting.seen) == repr(read_holding.seen)  # repr: an unknown series holds NaN
    assert stored(tmp_path / "resting.db") == stored(tmp_path / "holding.db")
    assert kinds_stored(tmp_path / "resting.db") == kinds_stored(tmp_path / "holding.db")
    assert all(rows for rows in kinds_stored(tmp_path / "holding.db").values())

    told = [LINE.fullmatch(line) for line in lines]
    assert all(told), lines
    assert [t[1] for t in told if t] == ["the session", "the review"]
    gone = dict(re.findall(r"([a-z ]+?) (\d+)(?:, |$)", told[0][2])) if told[0] else {}
    assert set(gone) == set(LET_GO) and all(int(n) > 0 for n in gone.values()), lines[0]
    assert re.fullmatch(r"reference quotes \d+, bars \d+, touch windows \d+", told[0][3] if told[0] else "")
    assert told[1] and told[1][2] == "nothing", "the after-market starts after the review"
    kept = [json.loads(run.detail) for run in resting.store.runs(REST_RUN, at("13:55:00"))]
    assert [k["after"] for k in kept] == ["the session", "the review"], "kept for the night's report"
    assert kept[0]["let_go"] == {name: int(n) for name, n in gone.items()} and kept[1]["let_go"] == {}
    assert holding.store.runs(REST_RUN, at("13:55:00")) == []
    resting.store.close()
    holding.store.close()


def test_nothing_is_let_go_while_a_window_is_still_to_be_answered() -> None:
    """Input: a view of every kind with the windows from 09:01 to 09:05 followed, the four-minute
    ones handed out up to 09:05 and the longer kinds' not (they end from 09:16 on). Output: nothing
    let go and nothing dropped; once every length's windows that end in the session are handed
    out, the book updates but one, the kinds' quotes and the trades go."""
    view = MarketView(TWO_DAYS, (SYMBOL,), Leaders(), at("09:00:00"), FEE, kinds=KINDS)
    for feed, row in with_sides(
        sorted(wandering(at("09:00:00"), at("09:09:00")), key=lambda e: e[1]["received_at"])
    ):
        view.on_event(feed, row)
    assert view.ended_windows(at("09:09:00")), "the four-minute windows from 09:01 to 09:05 are over"
    before = view.held()
    opened = at("09:00:00", NEXT_DAY)
    horizons = {k.horizon for k in KINDS}

    assert view.let_go(opened, horizons) == {}
    assert view.held() == before

    for horizon in horizons:
        view.ended_windows(at("15:20:00"), horizon)
    gone = view.let_go(opened, horizons)

    assert gone["book updates"] == before["book updates"] - 1
    assert gone["kind quotes"] == before["kind quotes"] > 0
    assert gone["trades"] == before["trades"] > 0
    after = view.held()
    assert (after["book updates"], after["kind quotes"], after["trades"]) == (1, 0, 0)
    assert after["bars"] == before["bars"] and after["touch windows"] == before["touch windows"]


def test_a_log_that_forgot_answers_the_same_from_there_on() -> None:
    """Input: twenty minutes of book updates in two books and their sizes in two running totals;
    one of each forgets what came before 09:10. Output: asked about 09:11 or later (the best
    quotes, the depth, the microprices at the minutes' ends, the order flow and the sums of any
    window from 09:10 on), both answer the same; the forgetful ones hold half, and say how many
    went."""
    updates = [e for e in wandering(at("09:00:00"), at("09:20:00")) if "bid_px_1" in e[1]]
    whole, cut = _Book(), _Book()
    totals, cut_totals = _Running(2, timedelta(minutes=30)), _Running(2, timedelta(minutes=30))
    for _, row in updates:
        for a_book in (whole, cut):
            a_book.add(row["received_at"], row)
        for running in (totals, cut_totals):
            running.add(row["received_at"], (float(row["bid_qty_1"]), 1.0))
    since = at("09:10:00")
    earlier = sum(1 for _, row in updates if row["received_at"] < since)

    assert cut.forget(since) == earlier and cut_totals.forget(since) == earlier
    assert len(cut._at) == len(whole._at) - earlier and len(cut_totals._at) == len(totals._at) - earlier

    minutes = [since + timedelta(minutes=m) for m in range(1, 11)]
    assert cut.at_minute_ends(minutes) == whole.at_minute_ends(minutes)
    for minute in minutes:
        assert cut.top(minute, since) == whole.top(minute, since)
        assert cut.depth(minute) == whole.depth(minute)
        assert cut.flow(since, minute) == whole.flow(since, minute)
        assert cut.flow(minute - timedelta(minutes=1), minute) == whole.flow(
            minute - timedelta(minutes=1), minute
        )
        assert cut_totals.between(since, minute) == totals.between(since, minute)
    late = book(at("09:20:30"), 81_200, 81_300, bid_qty=500, ask_qty=300)[1]
    for a_book in (whole, cut):
        a_book.add(late["received_at"], late)
    assert cut.flow(since, at("09:21:00")) == whole.flow(since, at("09:21:00"))

    everything = _Book()
    for _, row in updates:
        everything.add(row["received_at"], row)
    assert everything.forget(at("15:40:00")) == len(updates) - 1, "the last update stays"
    assert _Book().forget(since) == 0
