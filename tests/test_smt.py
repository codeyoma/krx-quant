"""SMT divergence (#520): the rolling-extreme SMT's rules on hand-made bars, the engine's live
judgement against the same day made again, and ``krx-quant smt``.

Every instrument's bar has a high of 1010 and a low of 990 (the futures 351 and 349) unless a test
says otherwise, so each look-back's extremes are those and each case below is worked out by hand.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.backfill import BAR_SCHEMA, FUTURES_BAR_SCHEMA, FUTURES_BARS_TR, MINUTE_BARS_TR, bars_key
from krx_quant.baselines import BASELINES
from krx_quant.cli import main
from krx_quant.engine import Event
from krx_quant.feeds import KOSPI200_FUTURES, TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore, smt_events
from krx_quant.recorder import ParquetRecorder
from krx_quant.reference import BAR, Bar
from krx_quant.smt import (
    BACKFILLED,
    BEARISH,
    BULLISH,
    FUTURES,
    LIVE,
    RECONSTRUCTED,
    RECORDED,
    Detector,
    Reconstruction,
    SmtEvent,
    closes,
    day_events,
    lines,
    reconstruct,
    replayed,
)
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import futures_trade, run, trade

A, B, F = "005930", "000660", FUTURES
DAY = date(2026, 10, 12)  # a Monday
NEXT = date(2026, 10, 13)
CALENDAR = MarketCalendar({DAY: True, NEXT: True})
USUAL = {A: (1010.0, 990.0), B: (1010.0, 990.0), F: (351.0, 349.0)}  # (high, low)
# An engine with no algorithm and no kind besides the direction: its baselines alone.
BARE: dict[str, Any] = {"algorithms": (), "kinds": (), "kind_algorithms": ()}
# What a test reads of an event: its bar close (HH:MM), pair, direction, L and swept side.
Found = tuple[str, str, str, str, int, str]


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def day_bars(
    changes: Mapping[tuple[str, str], tuple[float, float] | None] | None = None,
    *,
    first: str = "09:00",
    last: str = "15:19",
    day: date = DAY,
    levels: Mapping[str, tuple[float, float]] = USUAL,
) -> dict[str, list[Bar]]:
    """Each instrument's bar of every minute from ``first`` to ``last``, at its ``levels`` (high,
    low), but for ``changes`` by (instrument, minute): another (high, low), or None for no bar."""
    changes = changes or {}
    out: dict[str, list[Bar]] = {}
    for name in (A, B, F):
        bars, start = [], at(first, day)
        while start <= at(last, day):
            hhmm = f"{start:%H:%M}"
            if (name, hhmm) not in changes or changes[(name, hhmm)] is not None:
                high, low = changes.get((name, hhmm)) or levels[name]
                mid = (high + low) / 2
                bars.append(Bar(start, mid, high, low, mid, 10, 1, start, start))
            start += BAR
        out[name] = bars
    return out


def found(events: Sequence[SmtEvent]) -> set[Found]:
    return {(f"{e.bar_close:%H:%M}", e.a, e.b, e.direction, e.minutes, e.swept) for e in events}


def both(close: str, a: str, b: str, direction: str, swept: str) -> set[Found]:
    """The events of a pair at one bar close for both L."""
    return {(close, a, b, direction, minutes, swept) for minutes in (15, 30)}


def test_one_going_past_its_high_alone_is_a_bearish_smt_and_at_its_extreme_it_is_not_past() -> None:
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                # 005930 above its look-back's 1010; 000660 and the futures at theirs: not past.
                (A, "10:00"): (1011, 990),
                # Neither past: 005930 at its 1010 (a tie does not sweep), 000660 below its own.
                (B, "12:00"): (1009, 990),
                # Both stocks past: no SMT of theirs, but each with the futures.
                (A, "13:00"): (1011, 990),
                (B, "13:00"): (1011, 990),
            }
        ),
    )
    assert found(events) == (
        both("10:01", A, B, BEARISH, A)
        | both("10:01", A, F, BEARISH, A)
        | both("13:01", A, F, BEARISH, A)
        | both("13:01", B, F, BEARISH, B)
    )
    assert {(e.source, e.definition, e.version) for e in events} == {(RECONSTRUCTED, "rolling_extreme", 1)}


def test_one_going_past_its_low_alone_is_a_bullish_smt_whichever_side_it_is() -> None:
    events = day_events(CALENDAR, DAY, day_bars({(B, "10:00"): (1010, 989)}))
    assert found(events) == both("10:01", A, B, BULLISH, B) | both("10:01", B, F, BULLISH, B)


def test_a_bar_with_both_directions_has_no_verdict_and_leaves_its_episode_going_on() -> None:
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                (A, "09:59"): (1011, 990),  # bearish: an episode begins at 10:00
                (A, "10:00"): (1012, 980),  # past both its high and its low: no verdict
                (A, "10:01"): (1013, 990),  # bearish again: the same episode
                # 005930 past its high and 000660 past its low: no verdict for that pair, while
                # each makes its own SMT with the futures.
                (A, "11:00"): (1011, 990),
                (B, "11:00"): (1010, 989),
            }
        ),
    )
    assert found(events) == (
        both("10:00", A, B, BEARISH, A)
        | both("10:00", A, F, BEARISH, A)
        | both("11:01", A, F, BEARISH, A)
        | both("11:01", B, F, BULLISH, B)
    )


def test_an_episode_is_one_event_until_a_bar_close_without_it_rearms_it() -> None:
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                (A, "10:00"): (1011, 990),  # begins
                (A, "10:01"): (1012, 990),  # goes on
                (A, "10:02"): (1012, 990),  # at its extreme: no SMT, rearmed
                (A, "10:03"): (1013, 990),  # a new one
            }
        ),
    )
    assert found(events) == (
        both("10:01", A, B, BEARISH, A)
        | both("10:01", A, F, BEARISH, A)
        | both("10:04", A, B, BEARISH, A)
        | both("10:04", A, F, BEARISH, A)
    )


def test_the_side_that_sweeps_may_change_within_an_episode() -> None:
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                (A, "10:00"): (1011, 990),
                # 005930 at its new extreme, 000660 past its own: the pair's bearish episode goes
                # on (no event); 000660 and the futures begin one; 005930 and the futures rearm.
                (A, "10:01"): (1011, 990),
                (B, "10:01"): (1011, 990),
            }
        ),
    )
    assert found(events) == (
        both("10:01", A, B, BEARISH, A) | both("10:01", A, F, BEARISH, A) | both("10:02", B, F, BEARISH, B)
    )


def test_an_event_keeps_when_each_side_set_the_extreme_it_was_judged_against() -> None:
    """#521: at the event's first bar close, the start of each side's look-back bar holding its
    highest high (bearish) or lowest low (bullish), the latest of equal ones."""
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                (B, "09:40"): (1012, 990),  # in the 30 minutes before 10:00, not in the 15
                (A, "09:50"): (1015, 990),
                (A, "09:55"): (1015, 990),  # as high as 09:50: the later is kept
                (A, "10:00"): (1016, 990),  # past it alone: bearish
                (A, "10:50"): (1010, 985),
                (B, "11:00"): (1010, 989),  # past its low alone: bullish
            }
        ),
    )
    extremes = {
        (f"{e.bar_close:%H:%M}", e.a, e.b, e.direction, e.minutes): (
            f"{e.a_extreme:%H:%M}" if e.a_extreme else None,
            f"{e.b_extreme:%H:%M}" if e.b_extreme else None,
        )
        for e in events
    }
    # 000660's and the futures' bars are all at their extreme: the latest is the minute before.
    assert extremes[("10:01", A, B, BEARISH, 15)] == ("09:55", "09:59")
    assert extremes[("10:01", A, B, BEARISH, 30)] == ("09:55", "09:40")
    assert extremes[("10:01", A, F, BEARISH, 15)] == ("09:55", "09:59")
    assert extremes[("11:01", A, B, BULLISH, 15)] == ("10:50", "10:59")
    assert extremes[("11:01", B, F, BULLISH, 30)] == ("10:59", "10:59")
    assert all(None not in both_sides for both_sides in extremes.values())


def test_a_missing_bar_is_unknown_never_not_swept_and_leaves_an_episode_going_on() -> None:
    rising = {(A, f"10:{m:02d}"): (1011.0 + m, 990.0) for m in range(41)}  # past its high every minute
    events = day_events(
        CALENDAR,
        DAY,
        day_bars(
            {
                **rising,
                # Unknown while 000660's look-back holds it (to 10:21 for L 15, 10:36 for 30);
                # 005930 still past its high after: the same episode, no event.
                (B, "10:05"): None,
                # 005930 past its high while 000660 has no bar: unknown, not an SMT.
                (A, "13:00"): (1011, 990),
                (B, "13:00"): None,
            }
        ),
    )
    assert found(events) == (
        both("10:01", A, B, BEARISH, A) | both("10:01", A, F, BEARISH, A) | both("13:01", A, F, BEARISH, A)
    )


@pytest.mark.parametrize(
    ("day", "opens"),
    [(DAY, 9), (date(2027, 1, 4), 10)],  # a normal day; the year's first trading day opens at 10:00
)
def test_no_verdict_in_the_first_l_minutes_of_the_session_by_the_calendar(day: date, opens: int) -> None:
    calendar = MarketCalendar({day: True})
    session = calendar.session(day)
    assert session is not None and session.open.hour == opens

    def minute(m: int) -> str:
        return f"{opens:02d}:{m:02d}"

    events = day_events(
        calendar,
        day,
        day_bars(
            {
                # Its look-back would reach before the open (bars there: the futures from 08:45,
                # NXT's pre-market): no verdict.
                (A, minute(14)): (1011, 990),
                (A, minute(15)): (1012, 990),  # L 15's first verdict
                (A, minute(30)): (1013, 990),  # L 30's first verdict
            },
            first=f"{opens - 1:02d}:45",
            day=day,
        ),
    )
    assert found(events) == {
        (minute(16), A, B, BEARISH, 15, A),
        (minute(16), A, F, BEARISH, 15, A),
        *both(minute(31), A, B, BEARISH, A),
        *both(minute(31), A, F, BEARISH, A),
    }


def test_each_session_starts_afresh() -> None:
    detector = Detector(CALENDAR, RECONSTRUCTED)
    events = []
    for day, minute in ((DAY, "15:15"), (NEXT, "09:15")):
        session = CALENDAR.session(day)
        assert session is not None
        bars = day_bars({(A, minute): (1011, 990)}, day=day)
        events += [(day, e) for close in closes(session) for e in detector.at_close(close, bars)]
    # The episode going on at the day's last bar close does not hold the next day's first one back.
    assert {(day, f) for day, e in events for f in found([e])} == {
        *((DAY, f) for f in both("15:16", A, B, BEARISH, A) | both("15:16", A, F, BEARISH, A)),
        (NEXT, ("09:16", A, B, BEARISH, 15, A)),
        (NEXT, ("09:16", A, F, BEARISH, 15, A)),
    }


def minute_trades(
    prices: Mapping[tuple[str, str], float] | None = None,
    late: Mapping[tuple[str, str], timedelta] | None = None,
    *,
    last: str = "10:30",
) -> list[Event]:
    """A trade of each instrument in every minute from 09:00 to ``last``, at ten seconds past, at
    1000 (the futures 350) but for ``prices`` by (instrument, minute); received then, or ``late``
    by that much after its minute's end. In receipt order."""
    prices, late = prices or {}, late or {}
    events: list[Event] = []
    start = at("09:00")
    while start <= at(last):
        hhmm = f"{start:%H:%M}"
        traded = start + timedelta(seconds=10)
        for name in (A, B, F):
            received = start + BAR + late[(name, hhmm)] if (name, hhmm) in late else traded
            price = prices.get((name, hhmm), 350.0 if name == F else 1000.0)
            if name == F:
                event = futures_trade(traded, price)
                event[1]["received_at"] = received
            else:
                event = trade(received, int(price), traded=traded, symbol=name)
            events.append(event)
        start += BAR
    return sorted(events, key=lambda e: e[1]["received_at"])


def test_a_bar_received_after_its_bar_close_is_unknown_there() -> None:
    sweep = {(A, "10:00"): 1001.0}
    on_time = replayed(CALENDAR, DAY, minute_trades(sweep))
    assert found(on_time) == both("10:01", A, B, BEARISH, A) | both("10:01", A, F, BEARISH, A)
    # 000660's 10:00 trade arrives two seconds after 10:01: its bar is not in that bar close's.
    late = replayed(CALENDAR, DAY, minute_trades(sweep, {(B, "10:00"): timedelta(seconds=2)}))
    assert found(late) == both("10:01", A, F, BEARISH, A)


async def test_the_engine_judges_live_what_the_day_made_again_gives(tmp_path: Path) -> None:
    events = minute_trades(
        {(A, "10:00"): 1001.0, (B, "10:10"): 999.0, (F, "10:20"): 350.5},
        {(B, "10:00"): timedelta(seconds=2), (A, "10:15"): timedelta(seconds=1)},
    )
    engine = await run(tmp_path / "engine.sqlite3", events, at("10:40"), symbols=(A, B), **BARE)
    live = engine.store.smt_events(at("00:00"), at("00:00", NEXT))
    again = replayed(CALENDAR, DAY, events)
    assert found(again) == (
        both("10:01", A, F, BEARISH, A)
        | both("10:11", A, B, BULLISH, B)
        | both("10:11", B, F, BULLISH, B)
        | both("10:21", A, F, BEARISH, F)
        | both("10:21", B, F, BEARISH, F)
    )
    assert {e.source for e in live} == {LIVE}
    assert all(e.a_extreme and e.b_extreme for e in live), "kept with its extremes (#521)"
    assert [replace(e, source=RECONSTRUCTED) for e in live] == sorted(
        again, key=lambda e: (e.bar_close, e.a, e.b, e.direction, e.minutes)
    )
    engine.store.close()


async def test_a_failure_to_judge_smt_never_stops_the_bar_close_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fails(self: Detector, close: datetime, bars: Mapping[str, Sequence[Bar]]) -> list[SmtEvent]:
        raise RuntimeError("made to fail")

    monkeypatch.setattr(Detector, "at_close", fails)
    algorithm = FixedAlgorithm("fixed", features=("velocity",), unique=("velocity",))
    with caplog.at_level(logging.WARNING, logger="krx_quant.engine"):
        engine = await run(
            tmp_path / "engine.sqlite3",
            minute_trades({(A, "10:00"): 1001.0}),
            at("10:02"),
            symbols=(A, B),
            **{**BARE, "algorithms": (algorithm,)},
        )
    for symbol in (A, B):  # every predictor's prediction of the bar close is there, the algorithm's made
        made = {p["algorithm"]: p["status"] for p in engine.store.at_close(symbol, at("10:01"))}
        assert {*(b.name for b in BASELINES), "fixed"} <= set(made) and made["fixed"] == "ok"
    assert any("SMT not judged" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    assert engine.store.smt_events(at("00:00"), at("00:00", NEXT)) == []
    engine.store.close()


def test_kept_events_are_never_rewritten(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / DB_NAME)
    first = SmtEvent(at("10:01"), A, B, BEARISH, 15, A, LIVE)
    assert store.add_smt_events([first], at("10:01:00.2")) == 1
    assert store.add_smt_events([replace(first, swept=B)], at("10:05")) == 0
    assert store.smt_events(at("10:00"), at("10:02")) == [first]
    store.close()
    # Read beside an engine from before #520: no table, no events.
    old = sqlite3.connect(tmp_path / "old.sqlite3")
    assert smt_events(old, at("00:00"), at("00:00", NEXT)) == []
    old.close()


def test_a_table_kept_before_the_extremes_gains_them_and_its_rows_have_none(tmp_path: Path) -> None:
    """#521. Input: ``smt_events`` as #520 made it, with a row. Output: the store opens it, adds
    the columns, reads that row without extremes and keeps a new one's."""
    path = tmp_path / DB_NAME
    with sqlite3.connect(path) as before:
        before.executescript(
            """
            CREATE TABLE smt_events (definition TEXT NOT NULL, version INTEGER NOT NULL,
                bar_close TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, direction TEXT NOT NULL,
                minutes INTEGER NOT NULL, swept TEXT NOT NULL, source TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (definition, version, bar_close, a, b, direction, minutes, source));
            INSERT INTO smt_events VALUES ('rolling_extreme', 1, '2026-10-12T10:01:00+09:00',
                '005930', '000660', 'bearish', 15, '005930', 'live', '2026-10-12T10:01:00.2+09:00');
            """
        )
    before.close()
    store = PredictionStore(path)
    kept = SmtEvent(at("11:01"), A, B, BULLISH, 30, B, LIVE, a_extreme=at("10:50"), b_extreme=at("10:59"))
    store.add_smt_events([kept], at("11:01:00.2"))
    assert store.smt_events(at("10:00"), at("12:00")) == [
        SmtEvent(at("10:01"), A, B, BEARISH, 15, A, LIVE),
        kept,
    ]
    store.close()


def store_backfill(data: Path, day: date, bars: Mapping[str, Sequence[Bar]]) -> None:
    for name, each in bars.items():
        stock = name != F
        path = data / bars_key(day, MINUTE_BARS_TR if stock else FUTURES_BARS_TR, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = [
            {
                "symbol": name,
                "bar_time": b.start,
                **({"venue": "UN"} if stock else {"contract": "A01612"}),
                **{
                    k: int(getattr(b, k)) if stock else getattr(b, k)
                    for k in ("open", "high", "low", "close")
                },
                "volume": b.volume,
                "cum_value": 0,
            }
            for b in each
        ]
        pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA if stock else FUTURES_BAR_SCHEMA), path)


def record(data: Path, events: Sequence[Event]) -> None:
    feeds = {TRADE.tr_id: TRADE, KOSPI200_FUTURES.tr_id: KOSPI200_FUTURES}
    recorder = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    for tr_id, row in events:
        recorder.record(
            "events",
            row["received_at"].astimezone(KST).date(),
            tr_id,
            row["symbol"],
            row,
            feeds[tr_id].schema,
        )
    recorder.close()


def settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    data, state = tmp_path / "data", tmp_path / "state"
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))
    monkeypatch.setenv("KRX_SYMBOLS", f"{A},{B}")
    return data, state


def test_a_day_is_made_again_from_its_recording_before_its_backfilled_bars(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert reconstruct(data, DAY, CALENDAR) is None
    store_backfill(data, DAY, day_bars({(A, "10:00"): (1011, 990)}))
    from_bars = reconstruct(data, DAY, CALENDAR)
    assert from_bars is not None and from_bars.bars == BACKFILLED
    assert found(from_bars.events) == both("10:01", A, B, BEARISH, A) | both("10:01", A, F, BEARISH, A)
    events = minute_trades({(B, "10:10"): 999.0})
    record(data, events)
    from_recording = reconstruct(data, DAY, CALENDAR)
    assert from_recording is not None and from_recording.bars == RECORDED
    assert from_recording.events == replayed(CALENDAR, DAY, events)
    assert found(from_recording.events) == both("10:11", A, B, BULLISH, B) | both("10:11", B, F, BULLISH, B)
    # The Engine API's (#521): the backfilled bars first, the recording without them.
    first = reconstruct(data, DAY, CALENDAR, first=BACKFILLED)
    assert first is not None and (first.bars, first.events) == (BACKFILLED, from_bars.events)
    (data / bars_key(DAY, MINUTE_BARS_TR, B)).unlink()
    without = reconstruct(data, DAY, CALENDAR, first=BACKFILLED)
    assert without is not None and (without.bars, without.events) == (RECORDED, from_recording.events)


def test_the_smt_command_counts_a_day_beside_its_live_events_and_prints_no_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data, state = settings_env(monkeypatch, tmp_path)
    assert main(["smt", "--day", DAY.isoformat()]) == 1  # neither a recording nor bars
    assert "녹화도 채운 1분봉도 없다" in capsys.readouterr().out
    # Prices nothing else in the output could read as: 61,2xx for the stocks, 345.6x the futures.
    levels = {A: (61_240.0, 61_220.0), B: (61_239.0, 61_221.0), F: (345.67, 345.61)}
    bars = day_bars({(A, "10:00"): (61_241, 61_220), (B, "11:00"): (61_239, 61_219)}, levels=levels)
    store_backfill(data, DAY, bars)
    store = PredictionStore(state / DB_NAME)
    store.add_smt_events(
        [
            SmtEvent(at("10:01"), A, B, BEARISH, 15, A, LIVE),
            SmtEvent(at("14:00"), A, F, BULLISH, 30, F, LIVE),
        ],
        at("10:01"),
    )
    store.close()
    assert main(["smt", "--day", DAY.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "다시 계산: 채운 1분봉" in out
    assert f"  {A}-{B} {BEARISH} 15 {A}: 1, 1\n" in out
    assert f"  {A}-{F} {BULLISH} 30 {F}: 1, 0\n" in out
    assert f"  {B}-{F} {BULLISH} 30 {B}: 0, 1\n" in out
    assert "합계: live 2, reconstructed 8" in out
    assert "live와 견주기: 다르다. 같음 1, live에만 1, 다시 계산에만 7" in out
    assert f"  live에만: 14:00 {A}-{F} {BULLISH} 30 {F}\n" in out
    for price in ("6121", "6122", "6123", "6124", "345"):
        assert price not in out.replace(",", "")


def test_the_report_says_when_the_day_made_again_agrees_with_its_live_events() -> None:
    event = SmtEvent(at("10:01"), A, B, BEARISH, 15, A, RECONSTRUCTED)
    made = Reconstruction(DAY, RECORDED, [event])
    agreed = lines(DAY, made, [replace(event, source=LIVE)])
    assert "다시 계산: 녹화(엔진의 시장 보기로 재생)" in agreed
    assert f"  {A}-{B} {BEARISH} 15 {A}: 1, 1" in agreed
    assert agreed[-1] == "live와 견주기: 같다 (1건)"
    assert lines(DAY, made, [])[-1] == "live와 견주기: 그날 live 사건이 없다"
    # Live events of another definition or version are not this one's to compare.
    other = replace(event, source=LIVE, version=2)
    assert lines(DAY, made, [other])[-2:] == [
        "합계: live 0, reconstructed 1",
        "live와 견주기: 그날 live 사건이 없다",
    ]
