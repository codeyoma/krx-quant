"""The prediction kind axis (#179): kinds besides the direction, through the Replay Harness.

Input: hand-made events and a fake clock, the direction's algorithms and baselines as they
run, and the test kind (``tests/kind_doubles.py``: ``drift``, the mid-point at the end of the
four minutes against the bar close's) with stand-in algorithms of it. Output: the kind's
predictions and answers in their own tables, the Engine API and the stream, and the
direction's predictions, labels and leaderboard as they are without the kind.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant import predictions
from krx_quant.algorithms import RegistrationError, register_kind_algorithms
from krx_quant.api import create_app
from krx_quant.barriers import BARRIER
from krx_quant.cards import Meaning
from krx_quant.engine import Budget, Event
from krx_quant.features import Leaders
from krx_quant.feeds import BOOK_SNAPSHOT
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import (
    DIRECTION_KIND,
    NO_ANSWER,
    Answer,
    Followed,
    Kind,
    KindForecast,
    KindRegistrationError,
    QuoteLog,
    most_likely,
    register_kinds,
)
from krx_quant.market_view import MarketView
from krx_quant.predictions import PredictionStore
from krx_quant.pricing import Direction, tick_size
from krx_quant.ranges import RANGE
from krx_quant.replay import replay_into
from krx_quant.touch import RESTARTED, SILENT, TARGET_VERSION, TouchWindow
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.kind_doubles import DRIFT, SURE_RISE, TEST_KIND, FixedKindAlgorithm, drift_of
from tests.replay_events import CALENDAR, FEE, SYMBOL, api, at, book, get_json, run, steady_book, vi
from tests.test_replay import busy_morning, stored

DRIFTER = FixedKindAlgorithm("drifter", features=("velocity",), unique=("velocity",))


def kind_rows(db: Path, kind: str = DRIFT) -> list[tuple[Any, ...]]:
    """Every prediction of ``kind``, without row IDs."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT symbol, algorithm, model_version, bar_close, displayed_at, status, output"
            " FROM kind_predictions WHERE kind = ? ORDER BY bar_close, symbol, algorithm",
            (kind,),
        ).fetchall()


def answers(db: Path, kind: str = DRIFT) -> dict[str, dict[str, Any]]:
    """The kind's answers of the symbol, by bar close (HH:MM)."""
    store = PredictionStore(db)
    try:
        return {a["bar_close"][11:16]: a for a in store.kind_answers(kind, SYMBOL)}
    finally:
        store.close()


def touches(db: Path) -> dict[str, dict[str, Any]]:
    store = PredictionStore(db)
    try:
        return {t["bar_close"][11:16]: t for t in store.touches(SYMBOL)}
    finally:
        store.close()


# The registry.


def test_the_direction_is_the_first_kind_registered_as_it_always_ran() -> None:
    assert KINDS[0] is DIRECTION_KIND
    assert (DIRECTION_KIND.classes, DIRECTION_KIND.action) == (("up", "flat", "down"), ("up", "down"))
    assert (
        DIRECTION_KIND.answer_version == TARGET_VERSION == 3
    )  # the touch label (#170), crossed quotes counted (#187)
    ladder = ["barrier_15m", "barrier_30m", "barrier_60m"]
    assert [k.name for k in KINDS] == ["direction", "range", "barrier", *ladder], (
        "the range, then the barrier (#180, #183), then the barrier's horizon ladder (#263)"
    )


def kind(**changes: Any) -> Kind:
    fields: dict[str, Any] = {
        "name": "spread",
        "title": "t",
        "classes": ("narrow", "usual", "wide"),
        "class_titles": ("a", "b", "c"),
        "action": ("narrow", "wide"),
        "answer_version": 1,
        "answer": drift_of,
        **changes,
    }
    return Kind(**fields)


@pytest.mark.parametrize(
    ("refused", "why"),
    [
        ([kind(), kind()], "taken"),
        ([kind(name="Spread")], "lowercase"),
        ([kind(classes=("narrow", "narrow", "wide"))], "three"),
        ([kind(action=("huge",))], "action"),
        ([kind(fields={"p_wide": Meaning("x", "y", "z")})], "like probabilities"),
        ([kind(answer=None)], "answer function"),
        ([TEST_KIND, kind(baselines=TEST_KIND.baselines)], "taken"),
    ],
)
def test_a_kind_is_refused_when_it_breaks_the_registry_s_rules(refused: list[Kind], why: str) -> None:
    with pytest.raises(KindRegistrationError, match=why):
        register_kinds([DIRECTION_KIND, *refused])


def test_a_kind_s_algorithm_is_checked_within_its_kind_only() -> None:
    kinds = (DIRECTION_KIND, TEST_KIND)
    # velocity is Newton's own among the direction's: a drift algorithm may call it its own.
    assert register_kind_algorithms([DRIFTER], kinds, taken={"newton"})
    twin = FixedKindAlgorithm("twin", features=("velocity",), unique=("velocity",))
    with pytest.raises(RegistrationError, match="the same features as drifter"):
        register_kind_algorithms([DRIFTER, twin], kinds)
    with pytest.raises(RegistrationError, match="already registered"):
        register_kind_algorithms(
            [FixedKindAlgorithm("newton", features=("vwap_z",), unique=("vwap_z",))], kinds, {"newton"}
        )
    stray = FixedKindAlgorithm("stray", features=("vwap_z",), unique=("vwap_z",))
    stray.kind = "barrier"  # not registered
    with pytest.raises(RegistrationError, match="not a registered kind"):
        register_kind_algorithms([stray], kinds)


def test_a_forecast_keeps_its_kind_s_contract_or_is_an_error() -> None:
    """Input: forecasts of the test kind. Output: why each breaks its contract, if it does."""
    assert TEST_KIND.invalid(SURE_RISE) is None
    assert "distribution" in (TEST_KIND.invalid(KindForecast((0.5, 0.5, 0.5), {"drift_ticks": 0.0})) or "")
    assert "fields" in (TEST_KIND.invalid(KindForecast((0.2, 0.3, 0.5), {})) or "")
    assert "beyond" in (TEST_KIND.invalid(KindForecast((0.2, 0.3, 0.5), {"drift_ticks": 101.0})) or "")
    assert TEST_KIND.output(SURE_RISE) == {"p_fell": 0.1, "p_held": 0.2, "p_rose": 0.7, "drift_ticks": 1.0}


def test_a_forecast_s_class_is_the_most_likely_a_tie_the_middle_then_the_first() -> None:
    assert most_likely((0.2, 0.3, 0.5)) == 2
    assert most_likely((0.4, 0.4, 0.2)) == 1
    assert most_likely((0.4, 0.2, 0.4)) == 0
    assert most_likely((1 / 3, 1 / 3, 1 / 3)) == 1


# The window: the same quotes and rules as the touch label.

T = at("09:31:00")


def follow(
    quotes: list[tuple[float, float | None, float | None, bool]], *, crossed: bool = True
) -> tuple[TouchWindow, Followed]:
    """A touch window and a kind's window of the bar close T given the same (seconds after T,
    bid, ask, REST) quotes, some before it and after its end; the kind's counts crossed quotes,
    as the touch window does (#187), unless ``crossed`` is False."""
    touch = TouchWindow(SYMBOL, T, 81_250.0, FEE, bid_start=81_200.0, ask_start=81_300.0)
    log = QuoteLog()
    for seconds, bid, ask, rest in quotes:
        when = T + timedelta(seconds=seconds)
        touch.quote(when, bid, ask, rest=rest)
        log.quote(when, bid, ask, None, None, rest)
    return touch, log.window(
        SYMBOL, T, touch.anchor, touch.tick, touch.k, touch.bid_start, touch.ask_start, crossed=crossed
    )


@pytest.mark.parametrize(
    "quotes",
    [
        [(s, 81_200, 81_300, False) for s in range(0, 240, 30)],  # steady: scorable
        [(s, 81_200, 81_300, False) for s in (0, 30, 90, 180, 200)],  # 60 s without a quote
        [(s, 81_200, 81_300, False) for s in (0, 30, 60, 90, 120, 150, 179)],  # silent to the end
        [(s, 81_200, 81_300, s == 100) for s in range(0, 240, 20)],  # a REST snapshot
        [(s, 81_300 if s % 40 else 81_200, 81_200 if s % 40 else 81_300, False) for s in range(0, 240, 20)],
        [(s, 81_200, None if s == 40 else 81_300, False) for s in range(0, 240, 20)],  # one-sided
        [(-30, 81_200, 81_300, False), *((s, 81_200, 81_300, False) for s in range(20, 260, 20))],
    ],
)
def test_a_kind_s_window_reads_the_quotes_the_touch_label_reads_by_its_rules(
    quotes: list[tuple[float, float | None, float | None, bool]],
) -> None:
    """Input: quotes by receipt time around a window: steady ones, gaps of 60 s, a REST
    snapshot, crossed and one-sided ones, some before T and at or after T + 4 min. Output: the
    window of a kind that counts crossed quotes (#187) cannot be scored exactly when the touch
    window cannot, for the same reason, and holds the quotes the touch window took (received in
    [T, T + 4 min), two-sided, crossed or not); a kind that does not count them holds the same
    without the crossed ones."""
    touch, window = follow(quotes)

    assert window.unscorable == touch.result().unscorable
    taken = [
        (T + timedelta(seconds=s), bid, ask) for s, bid, ask, rest in quotes if 0 <= s < 240 and not rest
    ]
    assert [q.received_at for q in window.quotes] == [t for t, bid, ask in taken if bid and ask]
    _, uncrossed = follow(quotes, crossed=False)
    assert [q.received_at for q in uncrossed.quotes] == [
        t for t, bid, ask in taken if bid and ask and ask >= bid
    ]


def test_a_kind_that_drops_crossed_quotes_hears_silence_where_the_touch_label_hears_them() -> None:
    """Input: the book at the spread every 20 s, crossed from 09:31:40 to 09:32:40 (four quotes,
    a minute). Output: the touch window and a kind counting crossed quotes are scorable; a kind
    dropping them (the range) went 60 s without one of its quotes: silent quotes (#187)."""
    quotes: list[tuple[float, float | None, float | None, bool]] = [
        (s, 81_300, 81_200, False) if 40 <= s <= 100 else (s, 81_200, 81_300, False)
        for s in range(0, 240, 20)
    ]
    touch, counting = follow(quotes)
    _, dropping = follow(quotes, crossed=False)
    assert touch.result().unscorable is None and counting.unscorable is None
    assert dropping.unscorable == SILENT


def test_through_the_market_view_a_crossed_quote_touches_for_the_direction_and_the_barrier_only() -> None:
    """Input: the book at 81,200 / 81,300 every 10 s from 09:30:30 (the mid-point 81,250, k = 2;
    no trades, so the barrier's width is k), crossed at 09:32:05 at 81,500 / 81,400 (the bid at
    the upper target and at U, 81,450) and at 09:33:05 at 81,100 / 81,000 (the ask at the lower
    target and at L, 81,050). Output (#187): the touch label is up, both targets reached; the
    barrier answer up first at 09:32:05; the range answer as without the crossed quotes: small,
    r = 0 (their mid-points, 81,450 and 81,050, would have made it large)."""
    events = [
        *steady_book(at("09:30:30"), at("09:36:00"), 81_200, 81_300),
        book(at("09:32:05"), 81_500, 81_400),
        book(at("09:33:05"), 81_100, 81_000),
    ]
    view = MarketView(CALENDAR, (SYMBOL,), Leaders(), at("09:00:00"), FEE, kinds=(RANGE, BARRIER))
    for feed, row in sorted(events, key=lambda e: e[1]["received_at"]):
        view.on_event(feed, row)

    touch = view.touch(SYMBOL, T)
    assert (touch.direction, touch.upper, touch.lower) == (Direction.UP, 81_450, 81_050)
    assert touch.upper_reached is not None and touch.upper_reached.at == at("09:32:05")
    assert touch.lower_reached is not None and touch.lower_reached.at == at("09:33:05")
    barrier = view.kind_answer(BARRIER, SYMBOL, T)
    assert (barrier.label, barrier.path["w"], barrier.path["up_at"]) == (
        "up_first",
        2,
        at("09:32:05").isoformat(),
    )
    assert (barrier.path["up_bid"], barrier.path["up_ask"]) == (81_500, 81_400)
    ranged = view.kind_answer(RANGE, SYMBOL, T)
    assert (ranged.label, ranged.path["r"], ranged.path["max_mid"], ranged.path["min_mid"]) == (
        "small",
        0.0,
        81_250,
        81_250,
    )
    assert ranged.path["quotes"] == barrier.path["quotes"] - 2


def test_without_a_mid_point_at_the_bar_close_no_kind_can_score_the_window() -> None:
    window = QuoteLog().window(SYMBOL, T, None, None, None, None, None)
    answer = TEST_KIND.answer_to(window)
    assert (answer.label, answer.unscorable) == (None, "missing mid")


def test_an_answer_function_without_a_class_leaves_the_window_unscorable_with_its_reason() -> None:
    silent = kind(name="mute", answer=lambda w: Answer(None))
    log = QuoteLog()
    for s in range(0, 240, 20):
        log.quote(T + timedelta(seconds=s), 81_200, 81_300, None, None, False)
    window = log.window(SYMBOL, T, 81_250.0, 100, 2, 81_200.0, 81_300.0)
    assert silent.answer_to(window).unscorable == NO_ANSWER


# Through the Replay Harness.


def quiet(start: str, end: str) -> list[Event]:
    return steady_book(at(start), at(end), 81_200, 81_300, every=10)


async def run_kind(db: Path, events: list[Event], until: datetime, **options: Any) -> Any:
    options = {
        "kinds": (TEST_KIND,),
        "kind_algorithms": [DRIFTER],
        "target_version": TARGET_VERSION,
        **options,
    }
    return await run(db, events, until=until, **options)


async def test_a_kind_s_predictions_and_answers_are_stored_served_and_share_one_answer(
    tmp_path: Path,
) -> None:
    """Input: the book at 81,200 / 81,300 (mid 81,250), then from 09:32:30 at 81,400 / 81,500
    (mid 81,450). Output: the drift's predictions (its baselines and its algorithm) in
    ``kind_predictions``, one answer per bar close in ``kind_answers``: the windows from 09:29
    to 09:32 end above their bar close's mid (rose); 09:33 on, level (held). Every predictor
    of a bar close carries the same answer."""
    db = tmp_path / "engine.db"
    events = quiet("09:00:30", "09:32:20") + steady_book(
        at("09:32:30"), at("09:45:00"), 81_400, 81_500, every=10
    )

    engine = await run_kind(db, events, until=at("09:45:00"))

    labels = {close: a["class"] for close, a in answers(db).items()}
    assert labels["09:28"] == "held"
    assert [labels[c] for c in ("09:29", "09:30", "09:31", "09:32")] == ["rose"] * 4
    assert labels["09:33"] == "held"
    assert answers(db)["09:31"]["path"]["last_mid"] == 81_450
    async with api(engine) as client:
        latest = await get_json(client, f"/v1/symbols/{SYMBOL}/kinds/drift/latest")
        made = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?bar_close=2026-10-12T09:31:00%2B09:00"
        )
        recent = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?limit=3&algorithm=drifter"
        )
        later = await get_json(
            client,
            f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?bar_close=2026-10-12T09:33:00%2B09:00"
            "&algorithm=drift-persist",
        )
        kinds = await get_json(client, "/v1/kinds")
        listed = await get_json(client, "/v1/algorithms")
    check("kind_latest", latest)
    check("kind_predictions", made)
    assert latest["bar_close"] == "2026-10-12T09:45:00+09:00"
    by_name = {p["algorithm"]: p for p in made["predictions"]}
    assert set(by_name) == {"drift-held", "drift-persist", "drifter"}
    assert by_name["drifter"]["output"] == {"p_fell": 0.1, "p_held": 0.2, "p_rose": 0.7, "drift_ticks": 1.0}
    assert by_name["drift-held"]["output"]["p_held"] == 1.0
    assert by_name["drift-persist"]["output"]["p_held"] == 1.0, "the window of 09:27-09:31 held"
    [persist] = later["predictions"]
    assert persist["output"]["p_rose"] == 1.0, "the window of 09:29-09:33 rose"
    assert persist["output"]["drift_ticks"] == 2.0, "by 200 won, two ticks"
    assert {p["target_at"] for p in made["predictions"]} == {"2026-10-12T09:35:00+09:00"}
    answer = {(p["answer"]["class"], p["answer"]["answer_version"]) for p in made["predictions"]}
    assert answer == {("rose", 1)}, "one answer, every predictor's"
    assert [p["bar_close"][11:16] for p in recent["predictions"]] == ["09:45", "09:44", "09:43"]
    assert [k["name"] for k in kinds["kinds"]] == ["direction", "drift"]
    assert kinds["kinds"][1]["action"] == ["fell", "rose"]
    assert {a["name"]: a["kind"] for a in listed["algorithms"]}["drifter"] == "drift"
    assert {a["kind"] for a in listed["algorithms"] if a["name"] != "drifter"} == {"direction"}


async def test_the_direction_is_stored_scored_and_ranked_the_same_with_another_kind(tmp_path: Path) -> None:
    """Acceptance (#179). Input: the same busy morning replayed with and without the drift and
    an algorithm of it. Output: the direction's predictions and outcomes, its touch labels, and
    the Engine API's direction answers (predictions, latest, leaderboard, an algorithm's
    stats) are the same. Both run the range and the barrier: an algorithm's calls split by
    volatility read the barrier kind's widths (#262)."""
    without = await run(
        tmp_path / "without.db", busy_morning(), until=at("10:00:00"), target_version=TARGET_VERSION
    )
    # The direction's stats split calls by the barrier's width (#262): both engines run it.
    with_kind = await run_kind(
        tmp_path / "with.db", busy_morning(), until=at("10:00:00"), kinds=(RANGE, BARRIER, TEST_KIND)
    )

    direction = stored(tmp_path / "with.db")
    assert len(direction) == 60 * 15, "09:01-10:00: 4 baselines, 9 algorithms and 2 consensus"
    assert direction == stored(tmp_path / "without.db")
    assert touches(tmp_path / "without.db") == touches(tmp_path / "with.db")
    assert kind_rows(tmp_path / "with.db"), "the drift was predicted"
    paths = [
        f"/v1/symbols/{SYMBOL}/predictions?limit=1000",
        f"/v1/symbols/{SYMBOL}/latest",
        "/v1/leaderboard",
        "/v1/leaderboard?kind=direction",
        "/v1/leaderboard?period=5d&special=include",
        "/v1/algorithms/newton/stats",
        "/v1/algorithms/baseline-flat/stats",
    ]
    async with api(without) as one, api(with_kind) as two:
        for path in paths:
            assert await get_json(one, path) == await get_json(two, path), path


async def test_when_the_cpu_budget_runs_short_a_kind_s_algorithm_is_skipped_first(
    tmp_path: Path, fixed_costs: None
) -> None:
    """Input: two direction algorithms (0.05 s each) and a drift algorithm (0.2 s) registered
    after them, 0.3 s together, over a CPU budget of 0.28 s. Output: from 09:02 the drift's
    is skipped (and recorded) whenever they are over the budget together; the direction's run
    at every bar close."""
    first = FixedAlgorithm("first", features=("velocity",), unique=("velocity",), seconds=0.05)
    second = FixedAlgorithm("second", features=("acceleration",), unique=("acceleration",), seconds=0.05)
    heavy = FixedKindAlgorithm(
        "heavy", features=("relative_volume",), unique=("relative_volume",), seconds=0.2
    )

    engine = await run_kind(
        tmp_path / "engine.db",
        busy_morning(),
        until=at("09:03:30"),
        algorithms=[first, second],
        kind_algorithms=[heavy],
        budget=Budget(cpu=0.28),
    )

    with sqlite3.connect(tmp_path / "engine.db") as conn:
        skipped = conn.execute("SELECT at, algorithm FROM runs WHERE kind = 'cpu_budget'").fetchall()
        heavy_closes = [
            r[0][11:16]
            for r in conn.execute(
                "SELECT DISTINCT bar_close FROM kind_predictions WHERE algorithm = 'heavy' ORDER BY bar_close"
            )
        ]
        direction = conn.execute(
            "SELECT algorithm, COUNT(DISTINCT bar_close) FROM predictions"
            " WHERE algorithm IN ('first', 'second') GROUP BY algorithm"
        ).fetchall()
    assert skipped[0] == ("2026-10-12T09:02:00+09:00", "heavy")
    assert {name for _, name in skipped} == {"heavy"}
    assert "09:02" not in heavy_closes
    assert direction == [("first", 3), ("second", 3)]
    async with api(engine) as client:
        cpu = (await get_json(client, "/v1/status"))["cpu"]
    assert set(cpu["algorithms"]) == {"first", "second", "heavy"}


class Overreaching(FixedKindAlgorithm):
    """Breaks the drift's own check: ``drift_ticks`` beyond ±100."""

    def predict(self, features: Any, model: Any) -> KindForecast:
        return KindForecast((0.1, 0.2, 0.7), {"drift_ticks": 500.0})


def failing(window: Followed) -> Answer:
    raise RuntimeError("a broken answer function")


async def test_a_kind_that_fails_never_holds_up_the_direction(tmp_path: Path) -> None:
    """Input: a drift algorithm breaking its kind's contract, and a kind whose answer function
    raises. Output: the algorithm's predictions are errors, the broken kind stores no answer,
    and the direction is predicted and labelled as without them."""
    broken = kind(name="broken", answer=failing)
    over = Overreaching("over", features=("velocity",), unique=("velocity",))

    engine = await run_kind(
        tmp_path / "engine.db",
        busy_morning(end="09:20:00"),
        until=at("09:20:00"),
        kinds=(TEST_KIND, broken),
        kind_algorithms=[over],
    )
    await run(
        tmp_path / "without.db",
        busy_morning(end="09:20:00"),
        until=at("09:20:00"),
        target_version=TARGET_VERSION,
    )

    assert {r[5] for r in kind_rows(tmp_path / "engine.db") if r[1] == "over"} == {"error"}
    assert answers(tmp_path / "engine.db", "broken") == {}
    assert answers(tmp_path / "engine.db"), "the drift goes on"
    assert stored(tmp_path / "engine.db") == stored(tmp_path / "without.db")
    engine.store.close()


async def test_a_bar_close_in_a_vi_gets_empty_kind_predictions_and_a_special_answer(tmp_path: Path) -> None:
    """Input: a VI from 09:30:20, seen at 09:30:24 (taken to last 3 minutes, to 09:33:20), seen
    released at 09:32:40 at 09:32:44. Output: at 09:31 and 09:32 every drift predictor's
    prediction is ``vi`` (unscored) with its bar close's k (#200), mid-point and tick (#210), the
    windows still get their answers, and the answers of the windows the VI touches are special as
    the direction's predictions there are: 09:27 to 09:32 (09:33's bar close came after the
    release)."""
    events = sorted(
        quiet("09:00:30", "09:45:00")
        + [vi(at("09:30:24"), "09:30:20"), vi(at("09:32:44"), "09:30:20", "09:32:40")],
        key=lambda e: e[1]["received_at"],
    )

    engine = await run_kind(tmp_path / "engine.db", events, until=at("09:45:00"))

    async with api(engine) as client:
        made = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?bar_close=2026-10-12T09:31:00%2B09:00"
        )
        flat = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=baseline-flat&limit=1000")
    assert {p["status"] for p in made["predictions"]} == {"vi"}
    assert [p["answer"] for p in made["predictions"]] == [None] * 3
    assert {p["flat_ticks"] for p in made["predictions"]} == {2}, "k at 81,250, predicted or not"
    assert {(p["anchor"], p["tick"]) for p in made["predictions"]} == {(81_250.0, 100)}, (
        "the reference price there"
    )
    stored_answers = answers(tmp_path / "engine.db")
    assert stored_answers["09:31"]["class"] == "held"
    special = [c for c, a in sorted(stored_answers.items()) if a["special"]]
    assert special == ["09:27", "09:28", "09:29", "09:30", "09:31", "09:32"]
    direction = [
        p["bar_close"][11:16] for p in flat["predictions"] if p["outcome"] and p["outcome"]["special"]
    ]
    assert sorted(direction) == ["09:27", "09:28", "09:29", "09:30"], "09:31 and 09:32 were not predicted"


async def test_a_vi_learned_after_its_windows_were_answered_marks_their_answers_special(
    tmp_path: Path,
) -> None:
    """Input: a VI from 09:30:20 to 09:32:40 first seen at 09:40 (the status poll failed until
    then). Output: the answers of the windows it touched, 09:27 to 09:32, written before it was
    known, become special when it is; the direction's predictions there too."""
    events = sorted(
        quiet("09:00:30", "09:45:00") + [vi(at("09:40:00"), "09:30:20", "09:32:40")],
        key=lambda e: e[1]["received_at"],
    )

    engine = await run_kind(tmp_path / "engine.db", events, until=at("09:45:00"))

    special = [c for c, a in sorted(answers(tmp_path / "engine.db").items()) if a["special"]]
    assert special == ["09:27", "09:28", "09:29", "09:30", "09:31", "09:32"]
    async with api(engine) as client:
        flat = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=baseline-flat&limit=1000")
    direction = [
        p["bar_close"][11:16] for p in flat["predictions"] if p["outcome"] and p["outcome"]["special"]
    ]
    assert sorted(direction) == special


async def test_a_kind_s_window_the_engine_restarted_in_is_answered_so(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    events = quiet("09:00:30", "09:45:00")
    restart = at("09:33:30")

    await run_kind(db, [e for e in events if e[1]["received_at"] < restart], until=restart)
    await run_kind(
        db, [e for e in events if e[1]["received_at"] >= restart], until=at("09:45:00"), start=restart
    )

    stored_answers = answers(db)
    # 09:30 to 09:33 were predicted, their windows not over at the restart: not followed through.
    assert {stored_answers[c]["unscorable"] for c in ("09:30", "09:31", "09:32", "09:33")} == {RESTARTED}
    assert stored_answers["09:34"]["class"] == "held"  # opened after the restart
    assert stored_answers["09:29"]["class"] == "held"  # answered at 09:33, before it


async def test_a_rest_snapshot_or_a_silence_leaves_a_kind_s_answer_unscorable_as_the_touch_label(
    tmp_path: Path,
) -> None:
    """Input: the book every 10 s, a REST snapshot of it at 09:31:30, nothing from 09:38:00 to
    09:39:10. Output: each window's drift answer is unscorable exactly where, and why, its touch
    label is."""
    snapshot: Event = (
        BOOK_SNAPSHOT.tr_id,
        {"symbol": SYMBOL, "received_at": at("09:31:30"), "bid_px_1": 81_200, "ask_px_1": 81_300},
    )
    events = quiet("09:00:30", "09:38:00") + quiet("09:39:10", "09:50:00") + [snapshot]

    await run_kind(
        tmp_path / "engine.db", sorted(events, key=lambda e: e[1]["received_at"]), until=at("09:50:00")
    )

    labels, drift = touches(tmp_path / "engine.db"), answers(tmp_path / "engine.db")
    assert {c: t["unscorable"] for c, t in labels.items()} == {c: a["unscorable"] for c, a in drift.items()}
    assert drift["09:30"]["unscorable"] == "rest quotes"
    assert drift["09:36"]["unscorable"] == "silent quotes"


async def test_a_client_gets_a_kind_s_predictions_and_answers_as_they_are_made(tmp_path: Path) -> None:
    """Input: a stream client while the drift is predicted and answered. Output: ``kind_prediction``
    and ``kind_answer`` events (the kind named ``prediction_kind``), within the contract."""
    engine = await run_kind(tmp_path / "engine.db", [], until=at("09:00:00"))
    events = busy_morning(end="09:06:00")

    async with TestClient(TestServer(create_app(engine))) as client:
        ws = await client.ws_connect("/v1/stream")
        await _replay_more(engine, events, at("09:06:00"))
        received: list[dict[str, Any]] = []
        async with asyncio.timeout(5):
            while not any(e["type"] == "kind_answer" for e in received):
                received.append(await ws.receive_json())
                check("event", received[-1])
        await ws.close()

    made = [e for e in received if e["type"] == "kind_prediction"]
    assert {e["prediction_kind"] for e in made} == {"drift"}
    first = [p for e in made if e["bar_close"] == "2026-10-12T09:01:00+09:00" for p in e["predictions"]]
    assert {p["algorithm"] for p in first} == {"drift-held", "drift-persist", "drifter"}
    [answered] = [e for e in received if e["type"] == "kind_answer"]
    assert answered["at"] == "2026-10-12T09:05:00+09:00"
    assert [(a["symbol"], a["bar_close"]) for a in answered["answers"]] == [
        (SYMBOL, "2026-10-12T09:01:00+09:00")
    ]
    assert "kind" not in answered and all("kind" not in e for e in made)


async def _replay_more(engine: Any, events: list[Event], until: datetime) -> None:
    await replay_into(engine, engine.clock, events, until=until)


async def test_a_client_that_knows_only_the_old_events_sees_them_as_without_the_kind(tmp_path: Path) -> None:
    """For a screen from before #179: the events of the types it knows are the same with and
    without the drift; the drift's come as types it does not know (and ignores)."""

    async def heard(db: Path, **options: Any) -> list[dict[str, Any]]:
        engine = await run(db, [], until=at("09:00:00"), target_version=TARGET_VERSION, **options)
        async with TestClient(TestServer(create_app(engine, stream_poll_seconds=3600))) as client:
            ws = await client.ws_connect("/v1/stream")
            await _replay_more(engine, busy_morning(end="09:06:00"), at("09:06:00"))
            got: list[dict[str, Any]] = []
            async with asyncio.timeout(5):
                while not any(e["type"] == "outcome" and e["at"] == "2026-10-12T09:06:00+09:00" for e in got):
                    got.append(await ws.receive_json())
            await ws.close()
        return got

    without = await heard(tmp_path / "without.db")
    with_kind = await heard(tmp_path / "with.db", kinds=(TEST_KIND,), kind_algorithms=[DRIFTER])

    old = {"prediction", "consensus", "outcome", "health", "lifecycle", "bar"}

    def known(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
        """Each old event's type and content, its predictions as a set (algorithms finish in any order)."""
        out = []
        for e in events:
            if e["type"] not in old:
                continue
            items = e.get("predictions") or e.get("outcomes") or []
            out.append((e["type"], sorted(str(sorted(p.items())) for p in items)))
        return sorted(out)

    assert known(with_kind) == known(without)
    assert {e["type"] for e in with_kind} - old == {"kind_prediction", "kind_answer"}


async def test_an_engine_from_before_kinds_still_opens_the_database(tmp_path: Path) -> None:
    """Input: a database a kind was predicted into. Output: its schema version is the one before
    #179 (4), and an engine from before opens it as it opened its own (the version check and its
    schema script), reading the direction's predictions."""
    db = tmp_path / "engine.db"
    engine = await run_kind(db, busy_morning(end="09:10:00"), until=at("09:10:00"))
    engine.store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM kind_predictions").fetchone()[0] > 0
        # What an engine from before #179 runs when it opens the database: no kind table in it.
        assert "kind_" not in predictions._SCHEMA
        conn.executescript(f"BEGIN;{predictions._SCHEMA}PRAGMA user_version=4;COMMIT;")
        assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] > 0


# ``kind_predictions`` as an engine from before #200 makes it, without ``flat_ticks``, and its
# insert, naming the columns it knows.
KIND_PREDICTIONS_BEFORE_200 = """
CREATE TABLE IF NOT EXISTS kind_predictions (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT NOT NULL, algorithm TEXT NOT NULL,
    model_version TEXT NOT NULL, bar_close TEXT NOT NULL, displayed_at TEXT NOT NULL,
    status TEXT NOT NULL, output TEXT, UNIQUE (kind, symbol, algorithm, bar_close)
);
"""
INSERT_BEFORE_200 = (
    "INSERT INTO kind_predictions (kind, symbol, algorithm, model_version, bar_close, displayed_at, status,"
    " output) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def written_before_200(conn: sqlite3.Connection, bar_close: str) -> None:
    """A drift prediction of ``bar_close``, written as an engine from before #200 writes it."""
    output = '{"p_fell":0.0,"p_held":1.0,"p_rose":0.0,"drift_ticks":0.0}'
    conn.execute(INSERT_BEFORE_200, (DRIFT, SYMBOL, "drift-held", "v1", bar_close, bar_close, "ok", output))


async def test_the_bar_close_s_k_is_a_column_engines_before_and_after_it_both_open(tmp_path: Path) -> None:
    """#200. Input: a database an engine from before #200 made, with a drift prediction of the
    Friday before; the busy morning replayed into it with the drift; then a prediction written
    as an engine from before writes it. Output: the schema version stays 4; the engine adds the
    columns; the morning's drift predictions carry their bar close's k, the direction's k at the
    same bar close, and its mid-point and tick (#210), the direction's mid-point there; the
    Friday's and the one written as before have none (``null`` on the API)."""
    db = tmp_path / "engine.db"
    with sqlite3.connect(db) as conn:
        before = predictions._SCHEMA + KIND_PREDICTIONS_BEFORE_200
        conn.executescript(f"BEGIN;{before}PRAGMA user_version=4;COMMIT;")
        written_before_200(conn, "2026-10-09T15:00:00+09:00")

    engine = await run_kind(db, busy_morning(end="09:10:00"), until=at("09:10:00"))
    with sqlite3.connect(db) as conn:
        written_before_200(conn, "2026-10-12T09:11:00+09:00")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        made = {
            close: (k, anchor, tick)
            for close, k, anchor, tick in conn.execute(
                "SELECT DISTINCT bar_close, flat_ticks, anchor, tick FROM kind_predictions"
                " WHERE bar_close > '2026-10-12' AND bar_close < '2026-10-12T09:11'"
            )
        }
        direction = {
            close: (k, mid, tick_size(mid))
            for close, k, mid in conn.execute(
                "SELECT DISTINCT p.bar_close, o.flat_ticks, o.mid_close FROM predictions p JOIN outcomes o"
                " ON o.prediction_id = p.id"
            )
        }
    async with api(engine) as client:
        friday = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/drift/predictions?bar_close=2026-10-09T15:00:00%2B09:00"
        )
        latest = await get_json(client, f"/v1/symbols/{SYMBOL}/kinds/drift/latest")
    engine.store.close()

    assert len(made) == 10 and made == direction
    assert all(None not in terms for terms in made.values())
    check("kind_predictions", friday)
    check("kind_latest", latest)
    for before_it in (friday, latest):
        assert [(p["flat_ticks"], p["anchor"], p["tick"]) for p in before_it["predictions"]] == [
            (None, None, None)
        ]
    assert latest["bar_close"] == "2026-10-12T09:11:00+09:00"


def test_an_answer_is_stored_once_per_kind_symbol_bar_close_and_answer_version(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / "engine.db")
    rose = Answer("rose", None, {"x": 1})
    fell = Answer("fell", None, {"x": 2})
    assert store.add_kind_answers(DRIFT, 1, [(SYMBOL, T, rose, False)], at("09:35:00")) == [(SYMBOL, T)]
    assert store.add_kind_answers(DRIFT, 1, [(SYMBOL, T, fell, False)], at("09:36:00")) == []  # stays
    assert store.add_kind_answers(DRIFT, 2, [(SYMBOL, T, fell, False)]) == [(SYMBOL, T)]  # another version
    stored_answers = store.kind_answers(DRIFT, SYMBOL)
    store.close()
    assert [(a["answer_version"], a["class"], a["scored_at"]) for a in stored_answers] == [
        (1, "rose", "2026-10-12T09:35:00+09:00"),
        (2, "fell", "2026-10-12T09:35:00+09:00"),  # its four minutes' end
    ]
