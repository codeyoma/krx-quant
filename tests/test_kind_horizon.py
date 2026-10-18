"""A kind of a longer horizon (#263): its windows run from the bar close for its own minutes.

Input: the test kind ``drift`` (``tests/kind_doubles.py``: where the mid-point ends against the
bar close's) given a ten-minute horizon; books that step up at a known moment; a VI learned
late; bars that climb after a pause; a day's end. Output: when its answers are made and from
which quotes, what its baselines read, where it stops predicting before the closing auction,
which answers a late VI marks special, and what training and the bar rule give it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.features import Leaders
from krx_quant.kinds import DIRECTION_KIND, KindRegistrationError, QuoteLog, register_kinds
from krx_quant.rescore import answer_day
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import bar_answers, training_day
from tests.api_schema import check
from tests.kind_doubles import DRIFT, TEST_KIND
from tests.replay_events import CALENDAR, DAY, FEE, SYMBOL, api, at, get_json, steady_book, vi
from tests.synthetic_bars import day_of_bars
from tests.test_kinds import answers, kind, kind_rows, quiet, run_kind
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

TEN = timedelta(minutes=10)
LONG = replace(TEST_KIND, horizon=TEN)


def closing_auction() -> datetime:
    session = CALENDAR.session(DAY)
    assert session is not None
    return session.closing_auction


@pytest.mark.parametrize("horizon", [timedelta(minutes=3), timedelta(minutes=4, seconds=30)])
def test_a_horizon_is_whole_minutes_of_four_or_more(horizon: timedelta) -> None:
    with pytest.raises(KindRegistrationError, match="horizon"):
        register_kinds([kind(horizon=horizon)])
    with pytest.raises(KindRegistrationError, match="horizon"):
        register_kinds([replace(DIRECTION_KIND, horizon=TEN)])  # the direction's is four
    assert register_kinds([kind(horizon=TEN)])[0].horizon == TEN
    assert DIRECTION_KIND.horizon == timedelta(minutes=4)


def test_a_longer_window_reads_its_own_minutes_of_quotes() -> None:
    log = QuoteLog()
    t = at("09:31:00")
    for seconds in range(0, 900, 30):
        log.quote(t + timedelta(seconds=seconds), 81_200, 81_300, None, None, False)
    four = log.window(SYMBOL, t, 81_250.0, 100, 2, 81_200, 81_300)
    ten = log.window(SYMBOL, t, 81_250.0, 100, 2, 81_200, 81_300, horizon=TEN)
    assert (len(four.quotes), four.end) == (8, at("09:35:00"))
    assert (len(ten.quotes), ten.end) == (20, at("09:41:00"))
    assert ten.unscorable is None


def test_a_window_reads_its_quotes_by_bisection_or_by_a_scan_when_one_came_late() -> None:
    """The log is read by bisection while every quote came in receipt order; one received out
    of order (a later one first) leaves the same windows to a scan."""
    t = at("09:31:00")
    ordered, late = QuoteLog(), QuoteLog()
    for seconds in (0, 30, 60, 90, 240, 300):
        ordered.quote(t + timedelta(seconds=seconds), 81_200, 81_300, None, None, False)
    for seconds in (0, 60, 30, 90, 300, 240):
        late.quote(t + timedelta(seconds=seconds), 81_200, 81_300, None, None, False)
    for log in (ordered, late):
        four = log.window(SYMBOL, t, 81_250.0, 100, 2, 81_200, 81_300)
        assert sorted(q.received_at for q in four.quotes) == [
            t + timedelta(seconds=s) for s in (0, 30, 60, 90)
        ]
        assert len(log.window(SYMBOL, t, 81_250.0, 100, 2, 81_200, 81_300, horizon=TEN).quotes) == 6


async def test_a_longer_kind_is_answered_when_its_own_window_is_over(tmp_path: Path) -> None:
    """Input: the book at 81,200 / 81,300 (mid 81,250), from 09:32:30 at 81,400 / 81,500 (mid
    81,450). Output: ten-minute windows that reach past 09:32:30 end above their bar close's mid
    (09:23 to 09:32, rose); 09:22's ends before it (held); 09:33's starts there (held). By 09:50
    the windows to 09:40 are answered, none after. Its predictions' target time is ten minutes
    on, and the last window's answer goes on from ten minutes back (``drift-persist``)."""
    db = tmp_path / "engine.db"
    events = quiet("09:00:30", "09:32:20") + steady_book(
        at("09:32:30"), at("09:50:00"), 81_400, 81_500, every=10
    )

    engine = await run_kind(db, events, until=at("09:50:00"), kinds=(LONG,))

    labels = {close: a["class"] for close, a in answers(db).items()}
    assert labels["09:22"] == "held"
    assert [labels[f"09:{m}"] for m in range(23, 33)] == ["rose"] * 10
    assert labels["09:33"] == "held"
    assert max(labels) == "09:40", "the windows over by 09:50"
    async with api(engine) as client:
        made = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?bar_close=2026-10-12T09:33:00%2B09:00"
        )
        kinds = await get_json(client, "/v1/kinds")
    check("kind_predictions", made)
    check("kinds", kinds)
    by_name = {p["algorithm"]: p for p in made["predictions"]}
    assert {p["target_at"] for p in by_name.values()} == {"2026-10-12T09:43:00+09:00"}
    assert by_name["drift-persist"]["output"]["p_rose"] == 1.0, "the window of 09:23-09:33 rose"
    assert {k["name"]: k["horizon_minutes"] for k in kinds["kinds"]} == {"direction": 4, "drift": 10}


async def test_a_longer_kind_stops_predicting_where_its_window_would_reach_the_closing_auction(
    tmp_path: Path,
) -> None:
    """Input: a steady book through the afternoon to 15:30. Output: the direction predicts its
    last bar close four minutes before the closing auction; the ten-minute kind ten minutes
    before, and answers no later window (one would run into the auction)."""
    db = tmp_path / "engine.db"
    closing = closing_auction()
    events = steady_book(at("14:30:30"), at("15:30:00"), 81_200, 81_300, every=10)

    engine = await run_kind(db, events, until=at("15:40:00"), kinds=(LONG,))

    made = {row[3] for row in kind_rows(db)}
    assert max(made) == (closing - TEN).isoformat()
    assert max(answers(db)) == (closing - TEN).strftime("%H:%M")
    flat = engine.store.recent(SYMBOL, 1, "baseline-flat")
    assert flat[0]["bar_close"] == (closing - timedelta(minutes=4)).isoformat()
    engine.store.close()


async def test_a_vi_learned_late_marks_the_longer_windows_it_touched(tmp_path: Path) -> None:
    """Input: a VI from 09:30:20 to 09:32:40 first seen at 09:50. Output: the ten-minute
    windows it touched, 09:21 to 09:32, become special; four-minute ones would be 09:27 on."""
    events = sorted(
        quiet("09:00:30", "09:55:00") + [vi(at("09:50:00"), "09:30:20", "09:32:40")],
        key=lambda e: e[1]["received_at"],
    )

    await run_kind(tmp_path / "engine.db", events, until=at("09:55:00"), kinds=(LONG,))

    special = [c for c, a in sorted(answers(tmp_path / "engine.db").items()) if a["special"]]
    assert special == [f"09:{m}" for m in range(21, 33)]


async def test_a_recorded_day_trains_a_longer_kind_on_the_answers_the_engine_scores(tmp_path: Path) -> None:
    """Input: a busy morning, recorded, replayed with the ten-minute drift. Output: training's
    answers of each bar close are the engine's, for every window over by the replay's end."""
    events = busy_morning()
    data = tmp_path / "data"
    record(data, events)
    engine = await run_kind(
        tmp_path / "engine.db", events, until=at("10:00:00"), kinds=(LONG,), kind_algorithms=()
    )
    stored = {a["bar_close"]: a for a in engine.store.kind_answers(DRIFT, SYMBOL)}
    engine.store.close()

    day = training_day(
        data,
        DAY,
        CALENDAR,
        (SYMBOL,),
        ("velocity",),
        FEE,
        Leaders(),
        target_version=TARGET_VERSION,
        kinds=(LONG,),
    )

    assert day is not None
    samples = [s for s in day.of_kind(LONG, ("velocity",)) if s.bar_close <= at("09:50:00")]
    assert len(samples) == 50
    for s in samples:
        a = stored[s.bar_close.isoformat()]
        assert s.answer is not None
        assert (s.answer.label, s.answer.unscorable, dict(s.answer.path)) == (
            a["class"],
            a["unscorable"],
            a["path"],
        )


def test_the_bar_rule_reads_the_bars_of_its_own_horizon() -> None:
    """Input: bars from 09:00 flat to 09:09, then up a tick a minute. Output: at 09:05 the four
    minutes after stay flat (held) but the ten reach the climb (rose); none where a window would
    run into the closing auction."""
    bars = day_of_bars(DAY, [0.0] * 10 + [1.0] * 380)  # 09:00 to 15:29

    four = bar_answers({(SYMBOL, DAY): bars}, CALENDAR, FEE, (TEST_KIND,))
    ten = bar_answers({(SYMBOL, DAY): bars}, CALENDAR, FEE, (LONG,))

    t = at("09:05:00")
    held, rose = four[(DRIFT, SYMBOL, t)], ten[(DRIFT, SYMBOL, t)]
    assert held is not None and held.label == "held"
    assert rose is not None and rose.label == "rose"
    closing = closing_auction()
    assert (DRIFT, SYMBOL, closing - TEN) in ten
    assert (DRIFT, SYMBOL, closing - TEN + timedelta(minutes=1)) not in ten
    assert (DRIFT, SYMBOL, closing - timedelta(minutes=4)) in four


async def test_filling_in_a_day_answers_a_longer_kind_as_the_engine_does(tmp_path: Path) -> None:
    """Input: the busy morning replayed with the ten-minute drift, and the same day's events
    answered again (``rescore.answer_day``, #168). Output: the same answers, made at the end
    of each ten-minute window; none of a window that would reach the closing auction."""
    events = busy_morning()
    engine = await run_kind(
        tmp_path / "engine.db", events, until=at("10:00:00"), kinds=(LONG,), kind_algorithms=()
    )
    stored = {a["bar_close"]: (a["class"], a["unscorable"]) for a in engine.store.kind_answers(DRIFT, SYMBOL)}
    engine.store.close()

    filled = answer_day(
        sorted(events, key=lambda e: e[1]["received_at"]), CALENDAR, DAY, (SYMBOL,), FEE, (LONG,)
    )

    again = {
        a.bar_close.isoformat(): (a.answer.label, a.answer.unscorable) for a in filled if a.kind == DRIFT
    }
    assert {c: v for c, v in again.items() if c in stored} == stored
    assert max(again) <= (closing_auction() - TEN).isoformat()
