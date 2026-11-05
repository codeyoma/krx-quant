"""The trader's decisions through the Engine API (``GET /v1/trader/decisions``, #584,
``trader/decisions.py``), within the /v1 contract (``schemas/engine-api-v1.json``): each book's
apart, with the day's totals by reason, the cell's θ and p* in force, and whether the engine's answer
proved the call right; read-only, and no price.

Input: a paper day of the whole desk (the champion's book and a challenger's on the same predictions
and quotes), and a mock day's books written into the store as the trader writes them (``runs``,
``selections``, ``decisions``). Every price, probability and figure is made up.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.barriers import BARRIER as BARRIER_KIND
from krx_quant.barriers import DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.kinds import Answer
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.selection import Selection
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.api_schema import check
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.trader_doubles import (
    BARRIER,
    Barrier,
    Day,
    Event,
    Read,
    at,
    challenger,
    keep,
    pick,
    policy_cell,
    quote,
)

# The champion's θ 0.6 and the challenger's 0.65 on the barrier's breiman. Both buy the 10:00 rise and
# sell it at the target; neither buys the 10:05 call, which is not a rise; the champion alone buys the
# 10:10 rise (0.62), still held at 10:15, whose rise the challenger buys alone.
DESK_DAY: list[Event] = [
    Read("10:00:02.3", [Barrier("10:00:00", p=(0.7, 0.2, 0.1))]),
    quote("10:00:02.5", 70_000, 70_100),
    quote("10:02:00", 70_250, 70_300),  # both take their profit
    Read("10:05:02.3", [Barrier("10:05:00", p=(0.3, 0.4, 0.3))]),
    Read("10:10:02.3", [Barrier("10:10:00", p=(0.62, 0.2, 0.18))]),
    quote("10:10:02.5", 70_000, 70_100),
    Read("10:15:02.3", [Barrier("10:15:00", p=(0.7, 0.2, 0.1))]),
    quote("10:15:02.5", 70_000, 70_100),
]


def desk_day(state: Path) -> None:
    """``DESK_DAY`` on the whole desk, policy 1 the champion and policy 2 a challenger kept the night
    before; the engine answers the 10:00 call up first, the 10:05 one by its timeout and the 10:10 one
    down first; the 10:15 one is not answered yet."""
    keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
    challenger(state, at("2026-10-13 22:32:00"), policy_cell(BARRIER, 0.65))
    Day(state, desk=True).run(DESK_DAY)
    engine = PredictionStore(state / ENGINE_DB)
    try:
        engine.add_kind_answers(
            BARRIER_KIND.name,
            BARRIER_KIND.answer_version,
            [
                (SYMBOL, at("10:00:00"), Answer(UP_FIRST), False),
                (SYMBOL, at("10:05:00"), Answer(TIMEOUT), False),
                (SYMBOL, at("10:10:00"), Answer(DOWN_FIRST), False),
            ],
        )
    finally:
        engine.close()


async def decisions(state: Path, query: str = "", *, folder: bool = True) -> tuple[int, Any]:
    """The answer of an engine whose state folder is ``state`` (its own database replayed there
    apart), or with ``folder`` false of one without a state folder."""
    engine = await run(state / "replay.db", [], until=replay_at("09:00:00"))
    async with TestClient(TestServer(create_app(engine, state_dir=state if folder else None))) as client:
        response = await client.get(f"/v1/trader/decisions{query}")
        return response.status, await response.json()


def calls(book: dict[str, Any]) -> list[tuple[str, str, str | None, bool]]:
    """A book's decisions as (bar close's minute, action, reason, bought)."""
    return [
        (d["bar_close"][11:16], d["action"], d["reason"], d["trade_id"] is not None)
        for d in book["decisions"]
    ]


async def test_each_book_apart_newest_first_with_its_day_s_totals_and_answers(tmp_path: Path) -> None:
    desk_day(tmp_path)

    status, body = await decisions(tmp_path, "?day=2026-10-14")

    assert status == 200
    check("trader_decisions", body)
    assert (body["day"], body["book"]) == ("2026-10-14", None)
    champion, challenger = body["books"]
    assert [(b["book"], b["mode"], b["policy"], b["more"]) for b in body["books"]] == [
        ("champion", PAPER, 1, False),
        ("challenger", PAPER, 2, False),
    ]
    assert calls(champion) == [
        ("10:15", "skip", "holding", False),
        ("10:10", "buy", None, True),
        ("10:05", "skip", "not_up", False),
        ("10:00", "buy", None, True),
    ]
    assert calls(challenger) == [
        ("10:15", "buy", None, True),
        ("10:10", "skip", "below_threshold", False),
        ("10:05", "skip", "not_up", False),
        ("10:00", "buy", None, True),
    ]
    assert champion["totals"] == {
        "decisions": 4,
        "bought": 2,
        "waiting": 0,
        "unfilled": {},
        "skipped": {"holding": 1, "not_up": 1},
    }
    assert challenger["totals"]["skipped"] == {"below_threshold": 1, "not_up": 1}
    # The rise's probability, and the cell's θ and p* in each book's pick.
    assert {(d["probability"], d["threshold"], d["p_star"]) for d in champion["decisions"][1:2]} == {
        (0.62, 0.6, 0.6)
    }
    assert {d["threshold"] for d in challenger["decisions"]} == {0.65}
    # The engine's answers: up first is the rise, the timeout the middle, down first the fall.
    assert [(d["answer"], d["right"]) for d in champion["decisions"]] == [
        (None, None),
        ("down", False),
        ("flat", False),
        ("up", True),
    ]
    assert {d["decided_at"] for d in champion["decisions"]} == {
        "2026-10-14T10:15:02.300000+09:00",
        "2026-10-14T10:10:02.300000+09:00",
        "2026-10-14T10:05:02.300000+09:00",
        "2026-10-14T10:00:02.300000+09:00",
    }
    trades = {d["trade_id"] for d in champion["decisions"] if d["trade_id"] is not None}
    theirs = {d["trade_id"] for d in challenger["decisions"] if d["trade_id"] is not None}
    assert len(trades) == len(theirs) == 2 and not trades & theirs, "each book's own trades"

    (tmp_path / ENGINE_DB).unlink()
    _, unanswered = await decisions(tmp_path, "?day=2026-10-14&book=champion")
    check("trader_decisions", unanswered)
    assert {(d["answer"], d["right"]) for d in unanswered["books"][0]["decisions"]} == {(None, None)}, (
        "no engine database: no answer"
    )


async def test_a_book_asked_alone_and_a_day_without_decisions(tmp_path: Path) -> None:
    desk_day(tmp_path)

    _, theirs = await decisions(tmp_path, "?day=2026-10-14&book=challenger")
    _, none = await decisions(tmp_path, "?day=2026-10-14&book=practice")
    _, before = await decisions(tmp_path, "?day=2026-10-13")
    _, today = await decisions(tmp_path)

    check("trader_decisions", theirs)
    assert theirs["book"] == "challenger"
    assert [(b["book"], b["policy"]) for b in theirs["books"]] == [("challenger", 2)]
    assert none == {"day": "2026-10-14", "book": "practice", "books": []}, "a paper day has no practice"
    assert before["books"] == []
    assert today["day"] == "2026-10-12", "the engine's today"


# A mock day's books, written as the trader writes them: the run, each book's pick and its decisions.


def started(state: Path, mode: str, threshold: float) -> None:
    store = TraderStore(state / DB_NAME)
    try:
        store.add_run(at("08:00:00"), mode, {"threshold": threshold, "capital": 1_000_000})
    finally:
        store.close()


Decide = Callable[..., int]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraderStore]:
    kept = TraderStore(tmp_path / DB_NAME)
    try:
        yield kept
    finally:
        kept.close()


def decider(store: TraderStore) -> Decide:
    """A decision on the barrier's breiman ``prediction`` (its bar close two seconds before ``hms``)."""

    def decide(
        mode: str,
        policy: int | None,
        selection: int | None,
        hms: str,
        prediction: int,
        reason: str | None,
        action: str = "skip",
        probability: float = 0.7,
    ) -> int:
        decided = at(hms)
        close = decided.replace(second=0, microsecond=0)
        return store.add_decision(
            mode,
            SYMBOL,
            BARRIER.kind,
            BARRIER.predictor,
            prediction,
            close,
            close,
            decided,
            probability,
            action,
            reason,
            selection,
            policy,
        )

    return decide


def mock_day(state: Path, store: TraderStore) -> None:
    """Policy 1 the champion and 2 a challenger; the trader started in mock at θ 0.58. At 08:30 the
    champion had no cell to trade, and the mock book practised the cell kept for the cycle, a pick
    before #474 with no θ of its own: three not up, one bought to the day's limit, one cooling down. At
    10:30 the champion's cell traded again, at its θ 0.6: one not up, one buy that found no quote. Its
    paper shadow read the same: not up. The challenger skipped one below its θ 0.65; and a practice
    shadow from before #548 read one, which no book holds."""
    keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
    challenger(state, at("2026-10-13 22:32:00"), policy_cell(BARRIER, 0.65))
    started(state, MOCK, 0.58)
    practised = replace(pick(BARRIER, p_star=0.55, quantity=1), practice=True)
    cell = replace(pick(BARRIER, p_star=0.61), threshold=0.6)
    theirs = replace(pick(BARRIER, p_star=0.61), threshold=0.65)
    practice = store.add_selection(MOCK, at("08:30:00"), Selection((practised,), policy=1))
    champion = store.add_selection(MOCK, at("10:30:00"), Selection((cell,), policy=1))
    store.add_selection(PAPER, at("08:30:00"), Selection((), policy=1))  # the shadow's: no cell
    shadow = store.add_selection(PAPER, at("10:30:00"), Selection((cell,), policy=1))
    challenger_pick = store.add_selection(PAPER, at("08:30:00"), Selection((theirs,), policy=2))
    practice_shadow = store.add_selection(PAPER, at("08:30:00"), Selection((practised,), policy=1))
    decide = decider(store)
    for hms, prediction, reason in (
        ("10:00:02", 1, "not_up"),
        ("10:01:02", 2, "buy_limit"),
        ("10:02:02", 3, "not_up"),
        ("10:03:02", 4, "cooldown"),
        ("10:04:02", 5, "not_up"),
    ):
        decide(MOCK, 1, practice, hms, prediction, reason)
    decide(MOCK, 1, champion, "10:31:02", 31, "not_up")
    decide(MOCK, 1, champion, "10:32:02", 32, "no_quote", action="buy")
    decide(PAPER, 1, shadow, "10:31:02", 31, "not_up")
    decide(PAPER, 2, challenger_pick, "10:00:02", 1, "below_threshold", probability=0.62)
    decide(PAPER, 1, practice_shadow, "10:00:02", 1, "not_up")


async def test_a_mock_day_s_books_are_told_apart(tmp_path: Path, store: TraderStore) -> None:
    mock_day(tmp_path, store)

    _, body = await decisions(tmp_path, "?day=2026-10-14")

    check("trader_decisions", body)
    champion, practice, shadow, challenger = body["books"]
    assert [(b["book"], b["mode"], b["policy"]) for b in body["books"]] == [
        ("champion", MOCK, 1),
        ("practice", MOCK, None),
        ("shadow", PAPER, 1),
        ("challenger", PAPER, 2),
    ], "the practice of no policy; the practice shadow's decision in none"
    assert practice["totals"] == {
        "decisions": 5,
        "bought": 0,
        "waiting": 0,
        "unfilled": {},
        "skipped": {"not_up": 3, "buy_limit": 1, "cooldown": 1},
    }
    assert list(practice["totals"]["skipped"]) == ["not_up", "buy_limit", "cooldown"], "the commonest first"
    assert {(d["threshold"], d["p_star"]) for d in practice["decisions"]} == {(0.58, 0.55)}, (
        "a practice pick without its θ buys at the trader's"
    )
    assert champion["totals"] == {
        "decisions": 2,
        "bought": 0,
        "waiting": 0,
        "unfilled": {"no_quote": 1},
        "skipped": {"not_up": 1},
    }
    assert {(d["threshold"], d["p_star"]) for d in champion["decisions"]} == {(0.6, 0.61)}
    assert calls(shadow) == [("10:31", "skip", "not_up", False)]
    assert [(d["reason"], d["probability"], d["threshold"]) for d in challenger["decisions"]] == [
        ("below_threshold", 0.62, 0.65)
    ]

    for book, expected in (("champion", champion), ("practice", practice), ("shadow", shadow)):
        _, alone = await decisions(tmp_path, f"?day=2026-10-14&book={book}")
        assert alone["books"] == [expected]


async def test_the_books_before_the_morning_s_pick_are_those_of_the_night_before(
    tmp_path: Path, store: TraderStore
) -> None:
    """Challenger 2 is promoted on 10-14's night. On 10-15 the champion's book reads a call at 08:10,
    before its pick, under its 10-14 pick (policy 1); at the 08:30 pick the retired challenger skips
    one it was waiting on (``not_picked``) under its own; after it, the champion's book is policy 2's."""
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
    challenger(tmp_path, at("2026-10-13 22:32:00"), policy_cell(BARRIER, 0.65))
    champion = store.add_selection(PAPER, at("08:30:00"), Selection((pick(BARRIER),), policy=1))
    theirs = store.add_selection(PAPER, at("08:30:00"), Selection((pick(BARRIER),), policy=2))
    db = policies.connect(tmp_path / DB_NAME)
    try:
        policies.crown(db, at("2026-10-14 22:40:00"), 2, "승격", {})
    finally:
        db.close()
    promoted = store.add_selection(PAPER, at("2026-10-15 08:30:00"), Selection((pick(BARRIER),), policy=2))
    decide = decider(store)
    decide(PAPER, 1, champion, "2026-10-15 08:10:02", 1, "before_entry")
    decide(PAPER, 2, theirs, "2026-10-15 08:30:00", 2, "not_picked")
    decide(PAPER, 2, promoted, "2026-10-15 09:10:02", 3, "not_up")

    _, body = await decisions(tmp_path, "?day=2026-10-15")

    check("trader_decisions", body)
    assert [(b["book"], b["policy"], calls(b)) for b in body["books"]] == [
        ("champion", 1, [("08:10", "skip", "before_entry", False)]),
        ("champion", 2, [("09:10", "skip", "not_up", False)]),
        ("challenger", 2, [("08:30", "skip", "not_picked", False)]),
    ]


async def test_a_page_ends_after_every_decision_of_its_last_moment(
    tmp_path: Path, store: TraderStore
) -> None:
    """Two calls decided at 10:00:02 (the same bar close's, a rise waiting for the one ranked above
    it), one at 10:05:02: a page of one holds both 10:00:02 ones when it ends there."""
    decide = decider(store)
    decide(PAPER, None, None, "10:00:02", 1, "outranked")
    decide(PAPER, None, None, "10:00:02", 2, "not_up")
    decide(PAPER, None, None, "10:05:02", 3, "not_up")

    _, first = await decisions(tmp_path, "?day=2026-10-14&limit=1")
    _, second = await decisions(tmp_path, "?day=2026-10-14&limit=1&before=2026-10-14T10:05:02%2B09:00")
    _, last = await decisions(tmp_path, "?day=2026-10-14&before=2026-10-14T10:00:02%2B09:00")

    check("trader_decisions", first)
    [page] = first["books"]
    assert [d["prediction_id"] for d in page["decisions"]] == [3]
    assert page["more"] is True and page["totals"]["decisions"] == 3, "the totals are the whole day's"
    [page] = second["books"]
    assert [d["prediction_id"] for d in page["decisions"]] == [2, 1], "its moment's every decision"
    assert page["more"] is False
    assert [len(b["decisions"]) for b in last["books"]] == [0], "none before 10:00:02, the book still named"
    assert last["books"][0]["more"] is False
    assert all(d["threshold"] is None and d["p_star"] is None for d in page["decisions"]), "no pick"


async def test_the_trader_s_store_is_read_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route opens the trader's store and the engine's database read-only (``mode=ro``), changes
    neither, and makes no store where there is none. (A reader of the engine's database in its
    write-ahead-log mode leaves the log and its index beside it, as the running engine keeps them.)"""
    desk_day(tmp_path)

    def stored() -> dict[str, bytes]:
        return {
            p.name: p.read_bytes()
            for p in tmp_path.iterdir()
            if p.name.startswith(DB_NAME) or p.name == ENGINE_DB
        }

    files = stored()
    engine = await run(tmp_path / "replay.db", [], until=replay_at("09:00:00"))
    opened: list[str] = []
    connect = sqlite3.connect

    def spying(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if DB_NAME in str(database) or ENGINE_DB in str(database):
            opened.append(str(database))
        made: sqlite3.Connection = connect(database, *args, **kwargs)
        return made

    monkeypatch.setattr(sqlite3, "connect", spying)
    async with TestClient(TestServer(create_app(engine, state_dir=tmp_path))) as client:
        response = await client.get("/v1/trader/decisions?day=2026-10-14")
        assert response.status == 200
    async with TestClient(TestServer(create_app(engine, state_dir=tmp_path / "empty"))) as client:
        nothing = await (await client.get("/v1/trader/decisions?day=2026-10-14")).json()
    monkeypatch.undo()

    assert sorted(p.rsplit("/", 1)[-1] for p in opened) == [f"{ENGINE_DB}?mode=ro", f"{DB_NAME}?mode=ro"]
    assert all(p.startswith("file:") for p in opened)
    assert stored() == files
    assert nothing["books"] == [] and not (tmp_path / "empty").exists(), "no store made"


async def test_no_price_is_given(tmp_path: Path) -> None:
    """The decisions, their picks and their trades are of prices 70,000 to 70,300 (and a tick of 100):
    none of them, nor a field of a price, is in the answer."""
    desk_day(tmp_path)

    _, body = await decisions(tmp_path, "?day=2026-10-14")

    def keys(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for v in node.values() for k in keys(v)}
        if isinstance(node, list):
            return {k for v in node for k in keys(v)}
        return set()

    def numbers(node: Any) -> list[float]:
        if isinstance(node, dict):
            return [n for v in node.values() for n in numbers(v)]
        if isinstance(node, list):
            return [n for v in node for n in numbers(v)]
        return [node] if isinstance(node, int | float) and not isinstance(node, bool) else []

    priced = {
        "price",
        "anchor",
        "tick",
        "target",
        "stop",
        "reference_price",
        "bid",
        "ask",
        "mid",
        "width",
        "cost",
    }
    assert body["books"] and not keys(body) & priced
    assert keys(body["books"][0]["decisions"][0]) == {
        "id",
        "decided_at",
        "symbol",
        "kind",
        "predictor",
        "prediction_id",
        "bar_close",
        "displayed_at",
        "probability",
        "threshold",
        "p_star",
        "action",
        "reason",
        "trade_id",
        "answer",
        "right",
    }
    assert max(numbers(body)) < 100, "ids, counts and probabilities alone"
    assert not any(str(n) in json.dumps(body) for n in (70_000, 70_050, 70_100, 70_250, 70_300))


async def test_no_decisions_without_the_store_and_a_bad_query_is_refused(tmp_path: Path) -> None:
    assert await decisions(tmp_path, "?day=2026-10-14") == (
        200,
        {"day": "2026-10-14", "book": None, "books": []},
    )
    status, body = await decisions(tmp_path, "?day=2026-10-14", folder=False)
    assert (status, body["books"]) == (200, []), "an engine without a state folder (replay, demo)"
    for query in ("?day=2026-10-1", "?book=everyone", "?limit=0", "?limit=1001", "?before=10:00"):
        status, body = await decisions(tmp_path, query)
        assert status == 400, query
        check("error", body)
