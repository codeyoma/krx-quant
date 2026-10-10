"""Training on a recorded day: the engine's events turned into training samples.

Input: hand-made events, as the collector records them. Output: the samples, compared
with what the engine computed and scored when it replayed the same events.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from krx_quant.engine import Event
from krx_quant.features import FEATURES, Leaders, regime_code
from krx_quant.macro import MACRO, REGIME_SCHEMA
from krx_quant.pricing import Direction, tick_size
from krx_quant.recorder import ParquetRecorder
from krx_quant.training import recorded_samples, training_day
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, at, book, run, steady_book, trade, vi
from tests.test_algorithms_replay import LEADERS, leaders_morning
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

PEER = "000660"
NAMES = tuple(sorted(FEATURES))
END = at("10:30:00")


def received(event: Event) -> datetime:
    when: datetime = event[1]["received_at"]
    return when


def peer_morning() -> list[Event]:
    """A second symbol wandering its own way, so the peers lead each other."""
    events: list[Event] = []
    when, mid = at("09:00:03"), 201_000
    step = 0
    while when <= END:
        step += 1
        mid += 500 * ((step * 104_729) % 5 - 2)
        events.append(book(when, mid - 500, mid + 500, symbol=PEER))
        events.append(trade(when + timedelta(seconds=2), mid - 500, step % 4 + 1, symbol=PEER))
        when += timedelta(seconds=17)
    return events


def morning() -> list[Event]:
    return sorted([*busy_morning(end="10:30:00"), *peer_morning(), *leaders_morning()], key=received)


def scored(db: Path) -> dict[tuple[str, datetime], tuple[str | None, float | None, float | None]]:
    """Each scored prediction of the probe: class on basis A, mid-points at t and t + 4 minutes."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT p.symbol, p.bar_close, o.class_a, o.mid_close, o.mid_target"
            " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
            " WHERE p.algorithm = 'probe' AND o.scored_at IS NOT NULL"
        ).fetchall()
    return {(s, datetime.fromisoformat(t)): (c, m0, m4) for s, t, c, m0, m4 in rows}


async def test_a_recorded_day_trains_on_what_the_engine_saw_and_scored(tmp_path: Path) -> None:
    events = morning()
    probe = FixedAlgorithm("probe", features=NAMES, unique=NAMES)
    await run(
        tmp_path / "engine.db",
        events,
        until=END,
        symbols=(SYMBOL, PEER),
        algorithms=[probe],
        leaders=LEADERS,
    )

    samples = recorded_samples(events, CALENDAR, DAY, (SYMBOL, PEER), NAMES, FEE, LEADERS)

    predicted = [s for s in samples if s.bar_close <= END]
    assert len(predicted) == 2 * 90  # 09:01–10:30, both symbols
    assert [s.features.as_dict() for s in predicted] == probe.seen
    outcomes = scored(tmp_path / "engine.db")
    assert len(outcomes) == 2 * 86  # 10:27–10:30 wait for targets after the replay ends
    for s in predicted:
        if (s.symbol, s.bar_close) not in outcomes:
            continue
        class_a, mid_close, mid_target = outcomes[(s.symbol, s.bar_close)]
        assert (s.outcome.value if s.outcome else None) == class_a
        assert mid_close is not None and mid_target is not None
        assert s.change_ticks == (mid_target - mid_close) / tick_size(mid_close)
    assert {s.outcome for s in predicted} >= {Direction.UP, Direction.FLAT, Direction.DOWN}


def test_trades_bouncing_between_bid_and_ask_are_no_move_on_a_recorded_day() -> None:
    # The book stands still at 81,200 / 81,300; the last trade of each minute hits the
    # bid and the ask in turn. Closes would see a two-way move every minute.
    events = steady_book(at("09:00:00"), at("09:40:00"), 81_200, 81_300, every=5)
    for minute in range(40):
        price = 81_300 if minute % 2 else 81_200
        events.append(trade(at("09:00:30") + timedelta(minutes=minute), price))

    samples = recorded_samples(
        sorted(events, key=received), CALENDAR, DAY, (SYMBOL,), ("return_1",), FEE, Leaders()
    )

    outcomes = [s for s in samples if s.outcome is not None]
    assert len(outcomes) == 37  # 09:01–09:37: the last book, at 09:40, counts for a minute
    assert all(s.outcome is Direction.FLAT and s.change_ticks == 0 for s in outcomes)
    assert {s.features["return_1"] for s in outcomes[2:10]} != {0.0}, "the closes bounce"


async def test_a_recorded_day_leaves_out_what_live_statistics_leave_out(tmp_path: Path) -> None:
    # A VI from 09:30:20, first expected to last its least (3 minutes), released at 09:32:25.
    events = sorted(
        [*busy_morning(), vi(at("09:30:21"), "09:30:20"), vi(at("09:32:30"), "09:30:20", "09:32:25")],
        key=received,
    )
    await run(tmp_path / "engine.db", events, until=at("10:00:00"))

    samples = recorded_samples(events, CALENDAR, DAY, (SYMBOL,), ("return_1",), FEE, Leaders())

    with sqlite3.connect(tmp_path / "engine.db") as conn:
        rows = conn.execute(
            "SELECT p.bar_close FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
            " WHERE p.algorithm = 'baseline-flat' AND (p.status = 'vi' OR o.special = 1)"
        ).fetchall()
    left_out = {datetime.fromisoformat(t) for (t,) in rows}
    # 09:31 and 09:32 in the VI; 09:27–09:30 reach into it. By 09:33 the release is known.
    assert left_out == {at("09:27:00") + timedelta(minutes=m) for m in range(6)}
    trained = {s.bar_close for s in samples if s.bar_close <= at("10:00:00")}
    assert trained == {at("09:01:00") + timedelta(minutes=m) for m in range(60)} - left_out


def test_a_recorded_day_trains_in_the_macro_regime_decided_before_its_open(tmp_path: Path) -> None:
    data = tmp_path / "data"
    record(data, busy_morning(end="09:30:00"))
    parquet = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    for when, regime in (
        (at("07:30:00"), "risk_on"),
        (at("11:00:00"), "shock"),
    ):  # the second: after the open
        row = {
            "day": DAY,
            "recorded_at": when,
            "regime": regime,
            "reasons": "[]",
            "failed": "{}",
            "skipped": "[]",
        }
        parquet.record(MACRO, DAY, "regime", "market", row, REGIME_SCHEMA)
    parquet.close()

    day = training_day(data, DAY, CALENDAR, (SYMBOL,), ["macro_regime"], FEE, Leaders())

    assert day is not None and day.reference == "mid"
    assert {s.features["macro_regime"] for s in day.samples} == {regime_code("risk_on")}
