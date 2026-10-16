"""The prediction path through the Replay Harness.

Input: hand-made normalized events and a fake clock. Output: Engine API responses and
the predictions and outcomes stored in SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.engine import Event
from krx_quant.market_calendar import MarketCalendar
from krx_quant.names import logo_url
from tests.replay_events import (
    DAY,
    PREDICTING,
    PREDICTORS,
    SYMBOL,
    api,
    at,
    book,
    get_json,
    krx_trade,
    run,
    steady_book,
    trade,
)


async def test_every_baseline_predicts_at_a_bar_close_in_continuous_trading(tmp_path: Path) -> None:
    events = steady_book(at("09:29:00"), at("09:31:30"), bid=81_200, ask=81_300)

    engine = await run(tmp_path / "engine.db", events, until=at("09:31:30"))

    async with api(engine) as client:
        latest = await get_json(client, f"/v1/symbols/{SYMBOL}/latest")
    assert latest["bar_close"] == "2026-10-12T09:31:00+09:00"
    assert {p["algorithm"] for p in latest["predictions"]} == PREDICTORS
    for p in latest["predictions"]:
        assert p["bar_close"] == "2026-10-12T09:31:00+09:00"
        if p["algorithm"] in CONSENSUS:
            assert p["status"] == "no_members"  # no algorithm is active yet
            continue
        assert p["target_at"] == "2026-10-12T09:35:00+09:00"
        assert abs(p["p_up"] + p["p_flat"] + p["p_down"] - 1) < 1e-9
        assert p["outcome"] is None  # not scored before 09:35


def by_algorithm(predictions: list[dict[str, Any]], bar_close: str) -> dict[str, dict[str, Any]]:
    return {p["algorithm"]: p for p in predictions if p["bar_close"] == f"2026-10-12T{bar_close}+09:00"}


async def test_a_prediction_is_scored_on_its_bar_closes_touch_label_however_late_it_is_shown(
    tmp_path: Path,
) -> None:
    """Input: the mid-point 81,250 at the 09:31 bar close (k = 2: the upper target 81,450), the
    bid at 81,400 by the time the predictions are shown, 81,500 from 09:33. Output (#170): every
    predictor's outcome is the touch label, up (the bid reached 81,450 at 09:33), in
    ``class_a`` and ``class_b`` too for a screen from before; the mid-points stay as stored,
    and so do the old bases' classes in the DB (up from the bar close, flat from the display)."""
    events = [
        *steady_book(at("09:29:00"), at("09:31:00"), bid=81_200, ask=81_300),  # mid 81,250
        *steady_book(at("09:31:01"), at("09:32:51"), bid=81_400, ask=81_500),  # 81,450 when displayed
        *steady_book(at("09:33:00"), at("09:35:30"), bid=81_500, ask=81_600),  # 81,550 at the target
    ]

    engine = await run(
        tmp_path / "engine.db", events, until=at("09:35:30"), display_delay=timedelta(seconds=2)
    )

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
    at_close = by_algorithm(body["predictions"], "09:31:00")
    flat = at_close["baseline-flat"]
    assert flat["displayed_at"] == "2026-10-12T09:31:02+09:00"
    outcome = flat["outcome"]
    assert (outcome["mid_close"], outcome["mid_display"], outcome["mid_target"]) == (81_250, 81_450, 81_550)
    assert outcome["flat_ticks"] == 2
    assert (outcome["touch"]["class"], outcome["touch"]["upper_at"]) == ("up", "2026-10-12T09:33:00+09:00")
    assert (outcome["class_a"], outcome["class_b"]) == ("up", "up")
    assert outcome["unscorable"] is None
    assert outcome["special"] is False
    assert {p["outcome"]["class_a"] for p in at_close.values() if p["outcome"]} == {"up"}, "every predictor's"
    with sqlite3.connect(tmp_path / "engine.db") as conn:
        stored = conn.execute(
            "SELECT o.class_a, o.class_b FROM outcomes o JOIN predictions p ON p.id = o.prediction_id"
            " WHERE p.algorithm = 'baseline-flat' AND p.bar_close = '2026-10-12T09:31:00+09:00'"
        ).fetchone()
    # +300 won from the bar close is up, +100 won from the display flat: written, unread.
    assert stored == ("up", "flat")


async def test_a_prediction_whose_quotes_went_silent_is_left_unscorable(tmp_path: Path) -> None:
    # The book stops after 09:31:00: its window goes a minute and more without a quote.
    events = steady_book(at("09:29:00"), at("09:31:00"), bid=81_200, ask=81_300)

    engine = await run(tmp_path / "engine.db", events, until=at("09:36:00"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
    outcome = by_algorithm(body["predictions"], "09:31:00")["baseline-flat"]["outcome"]
    assert outcome["mid_close"] == 81_250
    assert outcome["mid_target"] is None
    assert (outcome["class_a"], outcome["class_b"]) == (None, None)
    assert outcome["unscorable"] == "silent quotes"  # the touch label's reason (#170)


async def test_without_any_quote_a_prediction_is_still_made_and_left_unscorable(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:36:00"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
    predictions = by_algorithm(body["predictions"], "09:31:00")
    assert set(predictions) == PREDICTORS
    outcome = predictions["baseline-persist"]["outcome"]
    assert outcome["flat_ticks"] is None
    assert outcome["unscorable"] == "missing mid"  # no mid-point at the bar close for the targets


def bar_closes(predictions: list[dict[str, Any]]) -> list[str]:
    return sorted({p["bar_close"][11:16] for p in predictions})


async def test_predictions_cover_continuous_trading_while_the_target_stays_inside_it(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("16:00:00"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
    closes = bar_closes(body["predictions"])
    # 09:00 is the open, not a bar close; 15:16 + 4 minutes is the last target before the
    # closing auction at 15:20.
    assert closes[0] == "09:01"
    assert closes[-1] == "15:16"
    assert len(closes) == 376


async def test_a_late_opening_day_starts_predicting_an_hour_later(tmp_path: Path) -> None:
    first_day = date(2026, 1, 2)  # the first trading day of 2026 opens at 10:00
    calendar = MarketCalendar({date(2026, 1, 1): False, first_day: True})

    engine = await run(tmp_path / "engine.db", [], until=at("12:00:00", first_day), calendar=calendar)

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
    assert bar_closes(body["predictions"])[0] == "10:01"


async def test_the_exam_day_moves_the_whole_window_an_hour_later(tmp_path: Path) -> None:
    exam_day = date(2026, 11, 19)  # 수능: 10:00–16:30, the closing auction from 16:20
    calendar = MarketCalendar({exam_day: True})

    engine = await run(tmp_path / "engine.db", [], until=at("17:00:00", exam_day), calendar=calendar)

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
    closes = bar_closes(body["predictions"])
    assert (closes[0], closes[-1], len(closes)) == ("10:01", "16:16", 376)


async def test_nothing_is_predicted_on_a_holiday(tmp_path: Path) -> None:
    holiday = date(2026, 10, 9)  # Hangul Day
    calendar = MarketCalendar({holiday: False})

    engine = await run(tmp_path / "engine.db", [], until=at("16:00:00", holiday), calendar=calendar)

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
        status = await get_json(client, "/v1/status")
    assert body["predictions"] == []
    assert status["phase"] == "closed"
    assert status["last_bar_close"] is None


async def mid_now(engine: Any) -> dict[str, Any]:
    async with api(engine) as client:
        body = await get_json(client, "/v1/symbols")
    [symbol] = body["symbols"]
    assert symbol["symbol"] == SYMBOL
    return dict(symbol)


async def test_only_krx_quotes_count_until_nxt_opens_thirty_seconds_in(tmp_path: Path) -> None:
    events = [
        book(at("09:00:05"), bid=81_000, ask=81_500),  # the integrated book, NXT not open yet
        krx_trade(at("09:00:10"), 81_200, bid=81_200, ask=81_300),
        book(at("09:00:35"), bid=81_300, ask=81_400),
    ]

    early = await run(tmp_path / "early.db", events, until=at("09:00:20"))
    later = await run(tmp_path / "later.db", events, until=at("09:00:40"))

    assert (await mid_now(early))["mid"] == 81_250
    assert (await mid_now(later))["mid"] == 81_350


async def test_a_locked_book_gives_its_price_and_a_crossed_one_is_ignored(tmp_path: Path) -> None:
    locked = [book(at("10:00:00"), bid=81_300, ask=81_300)]
    crossed = [*locked, book(at("10:00:05"), bid=81_400, ask=81_300)]

    assert await mid_now(await run(tmp_path / "locked.db", locked, until=at("10:00:10"))) == {
        "symbol": SYMBOL,
        "name": None,  # a replay names no company (#250)
        "logo_url": logo_url(SYMBOL),
        "mid": 81_300,
        "tick": 100,
        "flat_ticks": 2,
    }
    assert (await mid_now(await run(tmp_path / "crossed.db", crossed, until=at("10:00:10"))))["mid"] == 81_300


async def test_there_is_no_reference_price_outside_continuous_trading(tmp_path: Path) -> None:
    events = [book(at("08:55:00"), bid=81_200, ask=81_300), book(at("15:25:00"), bid=81_200, ask=81_300)]

    assert (await mid_now(await run(tmp_path / "pre.db", events, until=at("08:59:00"))))["mid"] is None
    assert (await mid_now(await run(tmp_path / "auction.db", events, until=at("15:25:30"))))["mid"] is None


async def test_integrated_trades_make_one_minute_bars_by_exchange_time(tmp_path: Path) -> None:
    events = [
        trade(at("09:30:05"), 81_200, 10),
        trade(at("09:30:20"), 81_400, 5),
        # Arrives after a later trade but happened first: it opens the bar.
        trade(at("09:30:30"), 81_000, 1, traded=at("09:30:02")),
        # Received after the minute ended, but traded inside it.
        trade(at("09:31:00.4"), 81_100, 3, traded=at("09:30:59")),
        trade(at("09:31:10"), 81_300, 2),  # the 09:31 bar has not finished
    ]

    engine = await run(tmp_path / "engine.db", events, until=at("09:31:30"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/bars")
    assert body["bars"] == [
        {
            "start": "2026-10-12T09:30:00+09:00",
            "open": 81_000,
            "high": 81_400,
            "low": 81_000,
            "close": 81_100,
            "volume": 19,
            "trades": 4,
        }
    ]


def forecast(prediction: dict[str, Any]) -> tuple[float, float, float, float]:
    return (prediction["p_up"], prediction["p_flat"], prediction["p_down"], prediction["expected_ticks"])


async def test_the_persistence_baseline_expects_the_last_four_minutes_to_go_on(tmp_path: Path) -> None:
    events = [
        *steady_book(at("09:26:40"), at("09:26:50"), bid=81_200, ask=81_300),  # 81,250 at 09:27
        *steady_book(at("09:30:30"), at("09:31:00"), bid=81_500, ask=81_600),  # 81,550 at 09:31
        *steady_book(at("09:31:30"), at("09:32:00"), bid=81_500, ask=81_600),  # 81,550 at 09:32
    ]

    engine = await run(tmp_path / "engine.db", events, until=at("09:32:30"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
    # +300 won over 09:27–09:31 is three 100-won ticks, past the 2-tick flat band: up.
    assert forecast(by_algorithm(body["predictions"], "09:31:00")["baseline-persist"]) == (1, 0, 0, 3)
    # Nothing known at 09:28 (no quote within the minute before), so no direction to go on.
    assert forecast(by_algorithm(body["predictions"], "09:32:00")["baseline-persist"]) == (0, 1, 0, 0)


async def test_the_random_baseline_draws_in_the_proportions_of_the_day_so_far(tmp_path: Path) -> None:
    # A book that never moves: every scored bar is flat.
    events = steady_book(at("09:00:30"), at("10:30:00"), bid=81_200, ask=81_300)

    engine = await run(tmp_path / "engine.db", events, until=at("10:30:00"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=baseline-random&limit=30")
    draws = [p for p in body["predictions"]]
    assert len(draws) == 30
    for p in draws:
        assert sorted(forecast(p)[:3]) == [0, 0, 1]  # one class, with certainty
    # By 10:00 sixty flat bars are scored, so flat is drawn about 61 times in 63.
    assert sum(p["p_flat"] for p in draws) >= 27


def busy_morning(day: date = DAY, end: str = "10:00:00") -> list[Event]:
    """A book and trades that wander, so every baseline and class shows up."""
    events: list[Event] = []
    when, mid = at("09:00:00", day), 81_250
    step = 0
    while when <= at(end, day):
        step += 1
        mid += 100 * ((step * 7919) % 5 - 2)  # a deterministic walk of -2..+2 ticks
        events.append(book(when, mid - 50, mid + 50))
        events.append(trade(when + timedelta(seconds=1), mid + 50, step % 9 + 1))
        when += timedelta(seconds=13)
    return events


def stored(db: Path) -> list[tuple[Any, ...]]:
    """Every prediction with its outcome, without row IDs: algorithms finish in any order."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT p.symbol, p.algorithm, p.model_version, p.bar_close, p.displayed_at, p.p_up, p.p_flat,"
            " p.p_down, p.expected_ticks, p.status, o.target_at, o.mid_close, o.mid_display, o.mid_target,"
            " o.flat_ticks, o.class_a, o.class_b, o.special, o.unscorable, o.scored_at"
            " FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id"
            " ORDER BY p.bar_close, p.symbol, p.algorithm"
        ).fetchall()


async def test_replaying_the_same_input_twice_gives_the_same_results(tmp_path: Path) -> None:
    first = await run(tmp_path / "first.db", busy_morning(), until=at("10:00:00"))
    second = await run(tmp_path / "second.db", busy_morning(), until=at("10:00:00"))

    assert stored(tmp_path / "first.db") == stored(tmp_path / "second.db")
    assert len(stored(tmp_path / "first.db")) == len(PREDICTORS) * 60  # 09:01–10:00
    async with api(first) as one, api(second) as two:
        for path in (f"/v1/symbols/{SYMBOL}/predictions?limit=1000", f"/v1/symbols/{SYMBOL}/bars"):
            assert await get_json(one, path) == await get_json(two, path)


def known_at_prediction(db: Path, t: str) -> list[tuple[Any, ...]]:
    """Each prediction at ``t`` with what was fixed when it was made: the mid-points it is
    scored from and the flat band.
    """
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT p.symbol, p.algorithm, p.model_version, p.bar_close, p.displayed_at, p.p_up, p.p_flat,"
            " p.p_down, p.expected_ticks, p.status, o.target_at, o.mid_close, o.mid_display, o.flat_ticks"
            " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id WHERE p.bar_close = ?"
            " ORDER BY p.algorithm",
            (f"2026-10-12T{t}+09:00",),
        ).fetchall()


async def test_a_prediction_does_not_change_when_everything_after_it_is_cut(tmp_path: Path) -> None:
    events = busy_morning()
    await run(tmp_path / "full.db", events, until=at("10:00:00"))
    # 09:13:00 has a book received exactly at the bar close.
    for t in ("09:05:00", "09:13:00", "09:31:00", "09:47:00"):
        cut = [e for e in events if e[1]["received_at"] <= at(t)]

        await run(tmp_path / f"cut-{t}.db", cut, until=at(t))

        known = known_at_prediction(tmp_path / f"cut-{t}.db", t)
        assert len(known) == len(PREDICTING)  # the empty consensus has nothing to score
        assert known == known_at_prediction(tmp_path / "full.db", t)


async def test_a_restarted_engine_scores_what_was_pending(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    events = steady_book(at("09:29:00"), at("09:36:00"), bid=81_200, ask=81_300)
    await run(db, [e for e in events if e[1]["received_at"] <= at("09:32:00")], until=at("09:32:00"))

    restarted = await run(
        db,
        [e for e in events if e[1]["received_at"] > at("09:33:00")],
        until=at("09:36:00"),
        start=at("09:33:00"),
    )

    async with api(restarted) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=baseline-flat")
    outcomes = {p["bar_close"][11:16]: p["outcome"] for p in body["predictions"]}
    # Predicted before the restart, scored after it at 09:35 and 09:36: their windows were not
    # followed through, so their touch labels say so.
    for close in ("09:31", "09:32"):
        assert outcomes[close]["scored_at"] is not None
        assert (outcomes[close]["class_a"], outcomes[close]["unscorable"]) == (None, "restarted")
    # 09:33 passed while the engine was down: no prediction for it.
    assert "09:33" not in outcomes


async def test_the_api_refuses_an_unknown_symbol_and_a_bad_limit(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:31:30"))

    async with api(engine) as client:
        unknown = await client.get("/v1/symbols/000000/latest")
        too_many = await client.get(f"/v1/symbols/{SYMBOL}/predictions?limit=5000")
        not_a_number = await client.get(f"/v1/symbols/{SYMBOL}/predictions?limit=ten")
        assert unknown.status == 404
        assert (await unknown.json())["error"] == "unknown symbol '000000'"
        assert too_many.status == 400
        assert not_a_number.status == 400


async def test_a_quote_received_right_at_a_bar_close_counts_after_it_everywhere(tmp_path: Path) -> None:
    events = [
        *steady_book(at("09:30:00"), at("09:30:50"), bid=81_200, ask=81_300),  # 81,250
        book(at("09:31:00"), bid=81_500, ask=81_600),  # received exactly at the 09:31 bar close
        *steady_book(at("09:31:10"), at("09:35:00"), bid=81_500, ask=81_600),  # 81,550
    ]

    engine = await run(tmp_path / "engine.db", events, until=at("09:35:30"))

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions")
    # The 09:31 prediction did not see the quote, so 09:31's mid stays 81,250 when 09:35
    # looks back four minutes to it: +300 won is up.
    assert by_algorithm(body["predictions"], "09:31:00")["baseline-flat"]["outcome"]["mid_close"] == 81_250
    assert forecast(by_algorithm(body["predictions"], "09:35:00")["baseline-persist"]) == (1, 0, 0, 3)
