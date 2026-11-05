"""A cell whose predictor is not active buys nothing until the next review (#603): a pick that finds a
policy's predictor on probation, on the bench, retired or a clone holds its cell off for the rest of
the review cycle, though it be active again meanwhile; its predictions are read and skipped
``predictor_inactive``, what it holds is sold by its own barriers, and no other cell (a clone's
original neither) takes its place. The champion's book, its shadow, a challenger's and the mock
practice alike; the final consensus and a baseline have no status. The re-fit's replay holds a cell off
on the days a pick would have, from the statuses as the engine kept them.

The first review cycle runs 10-14 to 11-10 (the calendar guessing the weekdays trade), the second from
11-11. The statuses change after a day's close, as the engine's review changes them (15:40); a test
that holds a position through a change moves it into the session, where a pick mid-session (a
restart) may find it. KIS is the fake one (``tests/test_trader_mock.py``). Every figure is made up.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from krx_quant import kis
from krx_quant.consensus import FINAL
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.lifecycle import Status
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import MOCK, PAPER, SYMBOL, practice, report, review
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import ACTIVE, Cell, inactive, open_engine_db, special_windows
from krx_quant.trader.decisions import CHALLENGER, CHAMPION, SHADOW, read_decisions
from krx_quant.trader.desk import Desk
from krx_quant.trader.paper import TAKE_PROFIT, Rules
from krx_quant.trader.replay import Ranked, Replayer, Source
from krx_quant.trader.selection import (
    GONE,
    NO_COST_MAP,
    NOT_IN_POLICY,
    PREDICTOR_INACTIVE,
    Selection,
    choose,
)
from krx_quant.trader.store import DB_NAME, TraderStore, read_trader
from tests.api_schema import check
from tests.kis_doubles import Fill, kis_mock, rows, settings_of
from tests.test_trader_replay import as_paper, models, of_paper
from tests.test_trader_shadow import BOUGHT, TAKEN, TEN, MockDesk, Repicked
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    DIRECTION,
    FEE,
    Barrier,
    Day,
    Direction,
    Read,
    Repick,
    at,
    challenger,
    cost_map,
    cost_row,
    first_champion,
    pick,
    policy_cell,
    practise,
    predictor,
    quote,
    record_quotes,
    whole_rules,
)

__all__ = ["kis_mock"]  # the fixture, used here

PERSIST = Cell("barrier", "barrier-persist")  # the barrier's baseline: no lifecycle status
ITO = Cell("barrier", "ito")


def status(state: Path, algorithm: str, when: str, to: Status, previous: Status | None = None) -> None:
    """``algorithm``'s status from ``when`` (``trader_doubles.at``), as the engine keeps a change."""
    engine = PredictionStore(state / ENGINE_DB)
    try:
        engine.add_status(algorithm, at(when), previous, to, "test")
    finally:
        engine.close()


def active_since_october(state: Path, algorithm: str = BARRIER.predictor) -> None:
    status(state, algorithm, "2026-10-01 15:40:00", Status.ACTIVE)


def cells_of(selection: dict[str, Any]) -> list[tuple[str, bool, str | None]]:
    """A kept pick's policy cells: (predictor, traded, why)."""
    return [(c["predictor"], c["traded"], c["why"]) for c in json.loads(selection["cells"])]


def test_the_lifecycle_s_active_status_is_the_one_that_buys() -> None:
    assert ACTIVE == Status.ACTIVE.value


# The champion's book and its shadow.


async def test_a_cell_whose_predictor_is_not_active_buys_nothing_and_its_position_sells_by_its_rules(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The champion (policy 1) trades the barrier's breiman on KIS's mock server, its shadow in paper.
    The 10:00 rise buys 14 on KIS and 14 in the shadow. breiman is benched at 10:00:30; the pick at
    10:01 (a restart's) finds it so: the 10:01 and 10:10 rises buy nothing (``predictor_inactive``),
    while the take-profits of what both books hold fill as ever (KIS's at 70,300, the shadow's at the
    target)."""
    fake, account = kis_mock
    first_champion(tmp_path)
    active_since_october(tmp_path)
    desk = MockDesk(tmp_path, fake, account, engine=True)

    def benched() -> None:
        status(tmp_path, BARRIER.predictor, "10:00:30", Status.BENCH, Status.ACTIVE)

    await desk.run(
        [
            TEN,
            BOUGHT,
            Repicked("10:01:00", Selection((pick(BARRIER),)), before=benched),
            Read("10:01:02.3", [Barrier("10:01:00")]),
            quote("10:01:02.5", 70_000, 70_100),
            TAKEN,
            Fill("10:02:30"),
            Read("10:10:02.3", [Barrier("10:10:00")]),
            quote("10:10:02.5", 70_000, 70_100),
        ]
    )

    trades = {(t["mode"], t["shadow"]): t for t in desk.trades(policy=1)}
    assert set(trades) == {(MOCK, False), (PAPER, True)}, "one each, the 10:00 rise's"
    assert trades[MOCK, False]["exit"]["reason"] == TAKE_PROFIT
    assert trades[MOCK, False]["exit"]["price"] == 70_300
    assert trades[PAPER, True]["exit"] == {
        "at": "2026-10-14T10:02:00+09:00",
        "price": 70_250,
        "reason": TAKE_PROFIT,
    }
    skipped = [
        (d["mode"], d["bar_close"][11:16], d["reason"])
        for d in rows(tmp_path, "decisions")
        if d["action"] == "skip"
    ]
    assert sorted(skipped) == [
        (MOCK, "10:01", PREDICTOR_INACTIVE),
        (MOCK, "10:10", PREDICTOR_INACTIVE),
        (PAPER, "10:01", PREDICTOR_INACTIVE),
        (PAPER, "10:10", PREDICTOR_INACTIVE),
    ]
    later = [p for p in rows(tmp_path, "selections") if p["at"] >= "2026-10-14T10:01"]
    assert later and all(cells_of(p) == [("breiman", False, PREDICTOR_INACTIVE)] for p in later)
    assert all((p["picks"], p["reason"]) == ("[]", NOT_IN_POLICY) for p in later)
    assert (
        "policy 1's cell barrier breiman is predictor_inactive: its predictor is bench, not active"
        in caplog.text
    )
    books = {
        b["book"]: b for b in read_decisions(tmp_path / DB_NAME, tmp_path / ENGINE_DB, date(2026, 10, 14))
    }
    for name in (CHAMPION, SHADOW):
        assert books[name]["totals"]["skipped"] == {PREDICTOR_INACTIVE: 2}
        inactive_skips = [d for d in books[name]["decisions"] if d["reason"] == PREDICTOR_INACTIVE]
        assert all(d["threshold"] is None and d["p_star"] is None for d in inactive_skips)


def test_a_cell_held_off_buys_nothing_until_the_next_review_though_active_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """breiman, the champion's cell and a challenger's, is benched after the 10-13 close and active again
    after the 10-14 close. The 10-14 start finds it on the bench: neither book buys its 10:00 rise. The
    10-15 pick finds it active, but the cycle holds it off still. The second cycle's first pick (11-11)
    trades it again. The decisions and the night's report say why."""
    first_champion(tmp_path)
    challenger(tmp_path, at("2026-10-13 22:32:00"), policy_cell(BARRIER))
    active_since_october(tmp_path)
    status(tmp_path, "breiman", "2026-10-13 15:40:00", Status.BENCH, Status.ACTIVE)
    status(tmp_path, "breiman", "2026-10-14 15:40:00", Status.ACTIVE, Status.BENCH)

    trades = Day(tmp_path, desk=True, gone=True, picks=(pick(BARRIER),)).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            Repick("2026-10-15 08:30:00"),
            Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
            quote("2026-10-15 10:00:02.5", 70_000, 70_100),
            Repick("2026-11-11 08:30:00"),
            Read("2026-11-11 10:00:02.3", [Barrier("2026-11-11 10:00:00")]),
            quote("2026-11-11 10:00:02.5", 70_000, 70_100),
        ]
    )

    [traded] = trades
    assert (traded["bar_close"], traded["policy"]) == ("2026-11-11T10:00:00+09:00", 1)
    picks = [p for p in rows(tmp_path, "selections") if p["policy_id"] == 1]
    assert [(p["cycle"], cells_of(p)) for p in picks] == [
        (1, [("breiman", False, PREDICTOR_INACTIVE)]),
        (1, [("breiman", False, PREDICTOR_INACTIVE)]),
        (2, [("breiman", True, None)]),
    ]
    skipped = [
        (d["policy_id"], d["bar_close"][:10], d["reason"])
        for d in rows(tmp_path, "decisions")
        if d["action"] == "skip"
    ]
    assert sorted(skipped) == [
        (1, "2026-10-14", PREDICTOR_INACTIVE),
        (1, "2026-10-15", PREDICTOR_INACTIVE),
        (2, "2026-10-14", PREDICTOR_INACTIVE),
        (2, "2026-10-15", PREDICTOR_INACTIVE),
    ]
    assert "its predictor is bench, not active" in caplog.text
    assert "its predictor was not active at a pick earlier this cycle" in caplog.text

    books = {
        b["book"]: b for b in read_decisions(tmp_path / DB_NAME, tmp_path / ENGINE_DB, date(2026, 10, 15))
    }
    assert books[CHAMPION]["totals"]["skipped"] == {PREDICTOR_INACTIVE: 1}
    assert books[CHALLENGER]["totals"]["skipped"] == {PREDICTOR_INACTIVE: 1}
    text = report.render(report.read(tmp_path / DB_NAME, date(2026, 10, 15), CALENDAR))
    assert "barrier breiman(top) predictor_inactive(not_in_policy)" in text
    assert "사지 않은 것: predictor_inactive 1" in text


def test_beside_the_cells_traded_the_report_names_those_held_off(tmp_path: Path) -> None:
    """The champion trades breiman and the barrier's baseline; breiman is on probation (a code change's
    restart): the baseline trades, and the report names breiman held off beside it."""
    first_champion(tmp_path, policy_cell(BARRIER), policy_cell(PERSIST, role="baseline"))
    status(tmp_path, "breiman", "2026-10-13 07:00:00", Status.PROBATION)

    trades = Day(tmp_path, desk=True, gone=True, picks=(pick(BARRIER), pick(PERSIST, role="baseline"))).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00"), Barrier("10:00:00", predictor=PERSIST.predictor)]),
            quote("10:00:02.5", 70_000, 70_100),
        ]
    )

    assert [t["predictor"] for t in trades] == [PERSIST.predictor]
    text = report.render(report.read(tmp_path / DB_NAME, date(2026, 10, 14), CALENDAR))
    assert "사지 않은 정책의 칸: barrier breiman(top) predictor_inactive" in text


# The consensus, a baseline, a clone.


def test_the_consensus_and_a_baseline_have_no_status_and_a_clone_is_held_never_its_original(
    tmp_path: Path,
) -> None:
    """The champion's cells, first ranked first: the barrier's breiman, a clone of ito; the final
    consensus; the barrier's baseline. shannon, a consensus member, is benched: the consensus follows
    its active members alone, as the engine makes it, and trades. breiman is held off; ito, its
    original, which the cost map names top and passes, is no cell of the champion's and never takes its
    place. Ranked first, breiman holds nothing back: the consensus's 10:00 rise buys at once."""
    first_champion(
        tmp_path,
        policy_cell(BARRIER),
        policy_cell(DIRECTION, role="consensus"),
        policy_cell(PERSIST, role="baseline"),
    )
    for name in ("ito", "shannon"):
        active_since_october(tmp_path, name)
    status(tmp_path, "breiman", "2026-10-13 15:40:00", Status.CLONE)
    status(tmp_path, "shannon", "2026-10-13 15:40:00", Status.BENCH, Status.ACTIVE)
    engine_db = tmp_path / ENGINE_DB
    assert inactive(engine_db, [BARRIER, DIRECTION, PERSIST, ITO], at("09:00:00")) == {BARRIER: "clone"}
    mapped = choose(
        cost_map(
            [
                cost_row(
                    "direction",
                    consensus=predictor(FINAL, 0.70, "pass"),
                    top=predictor("shannon", 0.50, "fail"),
                ),
                cost_row(
                    "barrier",
                    top=predictor("ito", 0.70, "pass"),
                    baseline=predictor(PERSIST.predictor, 0.55, "fail"),
                ),
            ]
        )
    )

    trades = Day(tmp_path, desk=True, gone=True, selection=mapped).run(
        [
            Read(
                "10:00:02.3",
                [Barrier("10:00:00"), Barrier("10:00:00", predictor="ito"), Direction("10:00:00", shown=1.0)],
            ),
            quote("10:00:02.5", 70_000, 70_100),
        ]
    )

    assert [t["predictor"] for t in trades] == [FINAL]
    assert trades[0]["entry"]["at"] == "2026-10-14T10:00:02.500000+09:00"
    [kept] = rows(tmp_path, "selections")
    assert cells_of(kept) == [
        ("breiman", False, PREDICTOR_INACTIVE),
        (FINAL, True, None),
        (PERSIST.predictor, True, None),
    ]
    [ito] = [c for c in json.loads(kept["considered"]) if c["predictor"] == "ito"]
    assert (ito["picked"], ito["why"]) == (False, "not in policy 1")
    decided = {(d["predictor"], d["action"], d["reason"]) for d in rows(tmp_path, "decisions")}
    assert ("breiman", "skip", PREDICTOR_INACTIVE) in decided
    assert not [d for d in decided if d[0] == "ito"], "never read: no book's cell"


def test_a_pick_without_a_cost_map_keeps_a_cell_held_off_and_a_gone_one_stays_gone() -> None:
    """A policy's cells under a cost map not read: one not active is held off all the same (the cycle
    keeps it), the others ``no_cost_map``. Under one read, a cell gone is ``gone`` before not active."""
    kept = policies.Policy(
        1,
        at("2026-10-13 22:31:00"),
        "test",
        (policy_cell(BARRIER), policy_cell(ITO), policy_cell(PERSIST, role="baseline")),
    )
    unread = policies.apply(kept, Selection((), reason=NO_COST_MAP), gone={ITO}, inactive={BARRIER, ITO})
    assert [(c["predictor"], c["why"]) for c in unread.cells] == [
        ("breiman", PREDICTOR_INACTIVE),
        ("ito", PREDICTOR_INACTIVE),
        (PERSIST.predictor, NO_COST_MAP),
    ]
    assert unread.inactive == (BARRIER, ITO) and unread.picks == ()
    read = policies.apply(kept, Selection((pick(BARRIER),)), gone={ITO}, inactive={BARRIER, ITO})
    assert [(c["predictor"], c["why"]) for c in read.cells] == [
        ("breiman", PREDICTOR_INACTIVE),
        ("ito", GONE),
        (PERSIST.predictor, None),
    ]
    assert read.inactive == (BARRIER,) and [p.cell for p in read.picks] == [PERSIST]


# The mock practice.


async def test_a_practice_cell_whose_predictor_is_not_active_rests_until_the_cycle_ends(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first cycle's practice cell is the barrier's breiman, put back on probation after the 10-13
    close: the 10-14 pick keeps when and practises nothing (``predictor_inactive``). Active again after
    the 10-14 close, it rests at the 10-15 pick still; the second cycle's cell is practised from its
    first pick."""
    fake, _ = kis_mock
    breiman = choose(cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])).practisable[0]
    practise(tmp_path, breiman)
    status(tmp_path, "breiman", "2026-10-13 15:40:00", Status.PROBATION)
    store = TraderStore(tmp_path / DB_NAME)
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(
                session,
                settings_of(fake, tmp_path),
                lambda: at("09:00:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )
            desk = Desk(
                store,
                whole_rules(),
                CALENDAR.session,
                at("09:00:00"),
                MOCK,
                broker=broker,
                clock=lambda: at("09:00:00"),
                practice=True,
                engine=tmp_path / ENGINE_DB,
            )
            desk.select(choose(failing), at("09:00:00"))
            rested = desk.champion.practice_state
            assert rested is not None
            assert (desk.champion.cells, rested["active"], rested["why"]) == ([], False, PREDICTOR_INACTIVE)
            assert rested["frozen"]["inactive_at"] == "2026-10-14T09:00:00+09:00"
            assert rested["frozen"]["gone_at"] is None
            desk.champion.write(at("09:00:00"))
            api = read_trader(tmp_path / DB_NAME, at("09:00:01"))
            check("trader", api)
            assert api["practice"]["why"] == PREDICTOR_INACTIVE

            status(tmp_path, "breiman", "2026-10-14 15:40:00", Status.ACTIVE, Status.PROBATION)
            desk.select(choose(failing), at("2026-10-15 08:30:00"))
            still = desk.champion.practice_state
            assert still is not None and desk.champion.cells == [] and still["why"] == PREDICTOR_INACTIVE

            practise(tmp_path, breiman, when="2026-11-10 22:40:00")  # the second cycle's, by the job
            desk.select(choose(failing), at("2026-11-11 08:30:00"))
            back = desk.champion.practice_state
            assert back is not None and desk.champion.cells == [BARRIER] and back["active"]
            await desk.close()
    finally:
        store.close()

    assert (
        "the practice cell barrier breiman is predictor_inactive: its predictor is probation, not active"
        in (caplog.text)
    )
    text = report.render(report.read(tmp_path / DB_NAME, date(2026, 10, 15), CALENDAR))
    assert "2026-10-14 09:00에 예측자가 active가 아니어서(predictor_inactive) 주기 끝까지 쉰다" in text
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        kept = practice.frozen(db, review.EVALUATION, 1)
    assert kept is not None and kept.inactive_at == at("09:00:00") and kept.rested


# The replay.

DAYS = (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16))


def three_days(state: Path) -> list[Any]:
    """A rise of breiman at 10:00 each day, bought at the first quote after it and sold at its target."""
    events: list[Any] = []
    for day in DAYS:
        when = f"{day.isoformat()} "
        events += [
            Read(f"{when}10:00:02.3", [Barrier(f"{when}10:00:00")]),
            quote(f"{when}10:00:02.5", 70_000, 70_100),
            quote(f"{when}10:02:00", 70_300, 70_400),
        ]
    return events


def replay_of(
    state: Path, evaluation: review.Evaluation | None
) -> tuple[list[Any], dict[str, int], Replayer]:
    trades: list[Any] = []
    db = open_engine_db(state / ENGINE_DB)
    try:
        replayer = Replayer(
            db,
            state / "data",
            CALENDAR.session,
            special_windows(db, SYMBOL, at("00:00:00")),
            {BARRIER: Source("top", DAYS)},
            rules=Rules(fee_rate=FEE),
            target_version=TARGET_VERSION,
            thresholds=(0.6,),
            evaluation=evaluation,
        )
        done = replayer.run([[Ranked(BARRIER, 0.6)]], alone=[BARRIER], record=trades.append)
    finally:
        db.close()
    return trades, dict(done.counts[BARRIER]), replayer


def test_the_replay_holds_a_cell_off_on_the_days_a_pick_would_have(tmp_path: Path) -> None:
    """breiman is benched after the 10-14 close and active again after the 10-15 close. The paper desk
    buys its 10-14 rise alone: the 10-15 pick finds it benched, and the 10-16 one holds it off for the
    cycle. The replay, on the statuses as the engine kept them and the trader's review cycles, makes the
    same trades; without the cycles, the 10-16 rise is bought, its predictor active at that day's pick."""
    models(tmp_path)
    first_champion(tmp_path)
    active_since_october(tmp_path)
    status(tmp_path, "breiman", "2026-10-14 15:40:00", Status.BENCH, Status.ACTIVE)
    status(tmp_path, "breiman", "2026-10-15 15:40:00", Status.ACTIVE, Status.BENCH)
    events = three_days(tmp_path)
    paper = Day(tmp_path, desk=True, gone=True, picks=(pick(BARRIER),), rules=Rules(fee_rate=FEE)).run(
        [*events, Repick("2026-10-15 08:30:00"), Repick("2026-10-16 08:30:00")]
    )
    record_quotes(tmp_path / "data", [e for e in events if not isinstance(e, Read)])
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        evaluation = review.current(db)
    assert evaluation is not None and evaluation.first_day == date(2026, 10, 14)

    trades, counts, replayer = replay_of(tmp_path, evaluation)

    assert of_paper(list(reversed(paper))) == as_paper(trades)
    assert [t.bar_close.date() for t in trades] == [date(2026, 10, 14)]
    assert replayer.inactive[BARRIER] == frozenset(DAYS[1:])
    assert counts["read"] == 3 and counts[PREDICTOR_INACTIVE] == 2

    alone, counts, replayer = replay_of(tmp_path, None)
    assert [t.bar_close.date() for t in alone] == [date(2026, 10, 14), date(2026, 10, 16)]
    assert replayer.inactive[BARRIER] == frozenset({date(2026, 10, 15)})
    assert counts[PREDICTOR_INACTIVE] == 1


def test_a_predictor_without_a_status_is_never_held_off_in_the_replay(tmp_path: Path) -> None:
    """The barrier's baseline has no lifecycle status: every day of it is replayed."""
    PredictionStore(tmp_path / ENGINE_DB).close()
    db = open_engine_db(tmp_path / ENGINE_DB)
    try:
        replayer = Replayer(
            db,
            tmp_path / "data",
            CALENDAR.session,
            [],
            {PERSIST: Source("baseline", DAYS)},
            rules=Rules(fee_rate=FEE),
            target_version=TARGET_VERSION,
            thresholds=(0.6,),
            evaluation=review.Evaluation(2, at("2026-10-13 22:30:00"), date(2026, 10, 14)),
        )
    finally:
        db.close()
    assert replayer.inactive == {PERSIST: frozenset()}
