"""Which cells may buy today (#646, ``trader/buyable.py``): the one answer the desk's books and the
replay ask for. A table of the cases the trader knows: a predictor not active, a predictor gone, a
cell resting for the rest of the review cycle, the practice taking over, and no cell at all.

Input: the trader's store and an engine database written as the trader and the engine write them
(``tests/trader_doubles.py``). The first review cycle runs 10-14 to 11-10. Every figure is made up.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import pytest

from krx_quant.lifecycle import Status
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, buyable, practice, replay, review
from krx_quant.trader.buyable import Rest, today
from krx_quant.trader.cells import Cell
from krx_quant.trader.selection import (
    GONE,
    NO_CHAMPION,
    NO_COST_MAP,
    NO_PASS,
    NO_POLICY,
    NOT_IN_POLICY,
    PREDICTOR_INACTIVE,
    Selection,
)
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    Barrier,
    at,
    first_champion,
    pick,
    policy_cell,
    practise,
)

PERSIST = Cell("barrier", "barrier-persist")  # a baseline: no lifecycle status
ITO = Cell("barrier", "ito")
PICK = "2026-10-15 08:30:00"


# The one function: a policy's cells less those resting.


@pytest.mark.parametrize(
    ("rest", "cost_map", "buys", "held", "whys"),
    [
        (Rest(), True, [BARRIER, ITO], [], [None, None]),
        (Rest(inactive=frozenset({BARRIER})), True, [ITO], [BARRIER], [PREDICTOR_INACTIVE, None]),
        (Rest(gone=frozenset({ITO})), True, [BARRIER], [], [None, GONE]),
        # Not active and gone alike: gone first, never read.
        (Rest(frozenset({BARRIER}), frozenset({BARRIER})), True, [ITO], [], [GONE, None]),
        # No cost map: none buys, but a cell not active is held still, so that the pick keeps it.
        (Rest(gone=frozenset({ITO})), False, [], [], [NO_COST_MAP, NO_COST_MAP]),
        (
            Rest(inactive=frozenset({ITO})),
            False,
            [],
            [ITO],
            [NO_COST_MAP, PREDICTOR_INACTIVE],
        ),
    ],
    ids=["all", "inactive", "gone", "gone-and-inactive", "no-cost-map-gone", "no-cost-map-inactive"],
)
def test_a_policy_s_cells_less_those_resting_buy_in_its_order(
    rest: Rest, cost_map: bool, buys: list[Cell], held: list[Cell], whys: list[str | None]
) -> None:
    cells = [replay.Ranked(BARRIER, 0.6), replay.Ranked(ITO, 0.7)]

    found = today(cells, rest, cost_map=cost_map)

    assert [r.cell for r in found.buys] == buys
    assert list(found.held) == held
    assert [why for _, why in found.rows] == whys
    assert all(r.threshold == {BARRIER: 0.6, ITO: 0.7}[r.cell] for r in found.buys)


def test_the_desk_and_the_replay_ask_the_same_function() -> None:
    """A policy's cells as the desk keeps them (``policy.PolicyCell``) and as the replay ranks them
    (``replay.Ranked``) rest alike."""
    rest = Rest(inactive=frozenset({BARRIER}))
    desk = today([policy_cell(BARRIER), policy_cell(ITO, 0.7)], rest)
    night = today([replay.Ranked(BARRIER, 0.6), replay.Ranked(ITO, 0.7)], rest)

    assert [(r.cell, r.threshold) for r in desk.buys] == [(r.cell, r.threshold) for r in night.buys]
    assert desk.held == night.held == (BARRIER,)
    assert vars(replay)["today"] is buyable.today
    assert vars(replay)["inactive_days"] is buyable.inactive_days


# The desk's questions, on the store and the engine's database.


def status(state: Path, algorithm: str, when: str, to: Status, previous: Status | None = None) -> None:
    engine = PredictionStore(state / ENGINE_DB)
    try:
        engine.add_status(algorithm, at(when), previous, to, "test")
    finally:
        engine.close()


def no_champion(state: Path) -> None:
    """Nothing kept: the evaluation begun, no champion."""
    store = TraderStore(state / DB_NAME)
    try:
        review.begin(store.db, at("2026-10-13 22:30:00"), CALENDAR.session)
    finally:
        store.close()


def champion_of_breiman(state: Path) -> None:
    first_champion(state)
    status(state, BARRIER.predictor, "2026-10-01 15:40:00", Status.ACTIVE)


def benched(state: Path) -> None:
    champion_of_breiman(state)
    status(state, BARRIER.predictor, "2026-10-14 15:40:00", Status.BENCH, Status.ACTIVE)


def benched_and_back(state: Path) -> None:
    """Benched after 10-13's close, active again after 10-14's: the 10-14 pick found it so, and the
    cycle rests it still at 10-15's."""
    champion_of_breiman(state)
    status(state, BARRIER.predictor, "2026-10-13 15:40:00", Status.BENCH, Status.ACTIVE)
    status(state, BARRIER.predictor, "2026-10-14 15:40:00", Status.ACTIVE, Status.BENCH)
    pick_at(state, at("2026-10-14 08:30:00"))


def gone_away(state: Path) -> None:
    """On 10-14 the engine made barrier predictions of barrier-persist alone."""
    champion_of_breiman(state)
    engine = PredictionStore(state / ENGINE_DB)
    try:
        Barrier("2026-10-14 10:00:00", predictor=PERSIST.predictor).write(engine)
    finally:
        engine.close()


def practised(state: Path) -> None:
    """No champion yet, the barrier's ito kept as the cycle's practice cell."""
    no_champion(state)
    practise(state, replace(pick(ITO, quantity=1), practice=True), threshold=0.65)


def practised_under_a_champion(state: Path) -> None:
    practised(state)
    first_champion(state)


def practised_but_benched(state: Path) -> None:
    practised(state)
    status(state, ITO.predictor, "2026-10-14 15:40:00", Status.BENCH, Status.ACTIVE)


def unreadable(state: Path) -> None:
    store = TraderStore(state / DB_NAME)
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO policies (created_at, reason, cells, left_out, fit)"
                " VALUES ('2026-10-13T22:31:00+09:00', 'x', 'not json', '[]', '{}')"
            )
            store.db.execute(
                "INSERT INTO champions (at, policy_id, reason) VALUES ('2026-10-13T22:31:00+09:00', 1, 'x')"
            )
    finally:
        store.close()


def pick_at(state: Path, when: datetime, selection: Selection | None = None, mode: str = PAPER) -> None:
    """A pick at ``when`` kept in the store, as the desk keeps it (the review cycle with it)."""
    store = TraderStore(state / DB_NAME)
    try:
        found = ask(store, state, when, selection, mode)
        store.add_selection(mode, when, found.under)
    finally:
        store.close()


def ask(
    store: TraderStore,
    state: Path,
    when: datetime,
    selection: Selection | None = None,
    mode: str = PAPER,
    practice_on: bool = False,
) -> buyable.Champion:
    evaluation = review.current(store.db)
    cycle = None if evaluation is None else review.cycle_at(evaluation, when, CALENDAR.session)
    chosen = Selection((pick(BARRIER),)) if selection is None else selection
    if cycle is not None:
        chosen = replace(chosen, evaluation=cycle.evaluation, cycle=cycle.number)
    return buyable.champion(
        store, chosen, cycle, when, engine=state / ENGINE_DB, mode=mode, practice=practice_on
    )


@dataclass(frozen=True)
class Case:
    given: Callable[[Path], None]
    selection: Selection | None  # None: the cost map passes the barrier's breiman
    practice_on: bool
    # The champion book's cells (predictor, θ), the shadow's (under the champion), held, the reason.
    chosen: list[tuple[str, float | None]]
    under: list[str]
    held: list[Cell]
    reason: str | None
    why: str | None = None  # ``GET /v1/trader``'s ``practice.why``
    cells: list[tuple[str, str | None]] | None = None  # the policy's cells: (predictor, why)


CASES = {
    "trades": Case(champion_of_breiman, None, False, [("breiman", 0.6)], ["breiman"], [], None,
                   cells=[("breiman", None)]),
    "predictor-inactive": Case(benched, None, False, [], [], [BARRIER], NOT_IN_POLICY,
                               cells=[("breiman", PREDICTOR_INACTIVE)]),
    "cycle-rests-it": Case(benched_and_back, None, False, [], [], [BARRIER], NOT_IN_POLICY,
                           cells=[("breiman", PREDICTOR_INACTIVE)]),
    "predictor-gone": Case(gone_away, None, False, [], [], [], NOT_IN_POLICY, cells=[("breiman", GONE)]),
    "no-champion": Case(no_champion, None, False, [], [], [], NO_CHAMPION),
    "no-cost-map": Case(champion_of_breiman, Selection((), reason=NO_COST_MAP, detail="down"), False,
                        [], [], [], NO_COST_MAP, cells=[("breiman", NO_COST_MAP)]),
    "no-policy": Case(unreadable, None, False, [], [], [], NO_POLICY),
    "practice": Case(practised, None, True, [("ito", 0.65)], [], [], NO_CHAMPION),
    "practice-not-frozen": Case(no_champion, None, True, [], [], [], NO_CHAMPION, practice.NOT_FROZEN),
    "practice-inactive": Case(practised_but_benched, None, True, [], [], [], NO_CHAMPION,
                              PREDICTOR_INACTIVE),
    "practice-after-no-pass": Case(practised, Selection((), reason=NO_PASS), True, [("ito", 0.65)], [], [],
                                   NO_CHAMPION),
    "no-practice-without-cost-map": Case(practised, Selection((), reason=NO_COST_MAP, detail="down"),
                                         True, [], [], [], NO_COST_MAP, NO_COST_MAP),
    "champion-over-practice": Case(practised_under_a_champion, None, True, [("breiman", 0.6)], ["breiman"],
                                   [], None, practice.CHAMPION),
}  # fmt: skip


@pytest.mark.parametrize("name", CASES)
def test_the_champion_s_pick(name: str, tmp_path: Path) -> None:
    case = CASES[name]
    case.given(tmp_path)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        mode = MOCK if case.practice_on else PAPER
        found = ask(store, tmp_path, at(PICK), case.selection, mode, case.practice_on)
    finally:
        store.close()

    assert [(p.predictor, p.threshold) for p in found.chosen.picks] == case.chosen
    assert [p.predictor for p in found.under.picks] == case.under
    assert list(found.under.inactive) == case.held
    assert found.under.reason == case.reason
    assert found.chosen.practising == (case.why is None and case.practice_on)
    if case.practice_on:
        assert found.practice["why"] == case.why
        assert found.practice["active"] == found.chosen.practising
    else:
        assert found.practice == buyable.practice_state(False)
    if case.cells is not None:
        assert [(c["predictor"], c["why"]) for c in found.under.cells] == case.cells
    if found.chosen.practising:
        [p] = found.chosen.picks
        assert (p.quantity, p.practice) == (1, True), "a share at a time, of no policy"
        assert found.chosen.inactive == ()


def test_a_challenger_rests_its_own_cells_and_never_practises(tmp_path: Path) -> None:
    benched(tmp_path)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        evaluation = review.current(store.db)
        assert evaluation is not None
        cycle = review.cycle_at(evaluation, at(PICK), CALENDAR.session)
        kept = store.read_policy(1)
        found = buyable.challenger(
            store, kept, Selection((pick(BARRIER),)), cycle, at(PICK), engine=tmp_path / ENGINE_DB
        )
    finally:
        store.close()

    assert (found.picks, found.inactive, found.reason) == ((), (BARRIER,), NOT_IN_POLICY)
    assert not found.practising


def test_the_practice_is_for_the_mock_mode_alone(tmp_path: Path) -> None:
    no_champion(tmp_path)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        with pytest.raises(ValueError, match="mock mode alone"):
            ask(store, tmp_path, at(PICK), mode=PAPER, practice_on=True)
    finally:
        store.close()
