"""The trader's strategy version frozen between reviews (#469, spec #468 "버전과 검토 주기"): the review
calendar of 20 trading days, kept across restarts; the champion's cells by their predictors' names,
a gone one trading no more until the next review; the model and evaluation versions on each trade;
the evidence of the evaluation version before kept and not inherited.

Input: the trader's store and an engine database written as the trader and the engine write them
(``tests/trader_doubles.py``), market calendars with made-up holidays. Every price and P&L is made up.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import SYMBOL, promotion, review
from krx_quant.trader import policy as policies
from krx_quant.trader.buyable import under_champion
from krx_quant.trader.cells import gone
from krx_quant.trader.promotion import BETWEEN_REVIEWS, REVIEWED
from krx_quant.trader.run import run_trader
from krx_quant.trader.selection import NOT_IN_POLICY, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore, read_trader
from tests.api_schema import check
from tests.promotion_doubles import LONG, WEEKDAYS, day_ran, judged_at, night, setup
from tests.server_doubles import free_port
from tests.test_trader_promotion import GOOD
from tests.test_trader_report import reported
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    Barrier,
    Day,
    Read,
    Repick,
    at,
    cost_map,
    cost_row,
    first_champion,
    pick,
    policy_cell,
    predictor,
    quote,
)
from tests.waiting import until

# Hangul Day, a Friday the market is closed.
HOLIDAYS = MarketCalendar({date(2026, 10, 9): False})


def evaluation_of(state: Path) -> review.Evaluation | None:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return review.current(db)


def rows(state: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


# The calendar.


def test_a_review_falls_every_twenty_trading_days_skipping_the_holidays(tmp_path: Path) -> None:
    """Begun at 06:00 of 10-01, before its open: the first cycle starts that day. Its trading days
    skip Hangul Day (10-09): its 20th is 10-29, not 10-28; the second cycle runs from 10-30 to its
    20th, 11-26. A day the market is closed belongs to the next trading day's cycle."""
    db = policies.connect(tmp_path / DB_NAME)
    try:
        evaluation = review.begin(db, at("2026-10-01 06:00:00"), HOLIDAYS.session)
    finally:
        db.close()
    assert (evaluation.version, evaluation.first_day, evaluation.review_days) == (2, date(2026, 10, 1), 20)

    def cycle(day: date) -> tuple[int, date, date, int | None]:
        found = review.cycle_of(evaluation, day, HOLIDAYS.session)
        return found.number, found.first, found.review, found.day

    first = (1, date(2026, 10, 1), date(2026, 10, 29))
    assert cycle(date(2026, 10, 1)) == (*first, 1)
    assert cycle(date(2026, 10, 9)) == (*first, 7), "the holiday: the next trading day's, 10-12, the 7th"
    assert cycle(date(2026, 10, 29)) == (*first, 20)
    assert cycle(date(2026, 10, 30)) == (2, date(2026, 10, 30), date(2026, 11, 26), 1)
    assert cycle(date(2026, 9, 30)) == (*first, None), "before the first day: the first cycle, no place"
    assert review.cycle_of(evaluation, date(2026, 10, 1), CALENDAR.session).review == date(2026, 10, 28), (
        "without the holiday, a day sooner"
    )


def test_the_review_is_judged_after_its_close_and_applies_from_the_next_trading_day(tmp_path: Path) -> None:
    """The review day 10-29: its night runs from its close to the next trading day's pick, and the
    cycle in force after the close is the next one."""
    db = policies.connect(tmp_path / DB_NAME)
    try:
        evaluation = review.begin(db, at("2026-10-01 06:00:00"), HOLIDAYS.session)
    finally:
        db.close()

    def reviewing(moment: str) -> date | None:
        return review.reviewing(evaluation, at(moment), HOLIDAYS.session)

    assert reviewing("2026-10-29 15:29:59") is None, "the session: not yet"
    assert reviewing("2026-10-29 15:30:00") == date(2026, 10, 29)
    assert reviewing("2026-10-30 07:00:00") == date(2026, 10, 29), "its night until the next pick"
    assert reviewing("2026-10-30 08:30:00") is None
    assert reviewing("2026-10-28 22:30:00") is None, "not a review day"
    assert review.cycle_at(evaluation, at("2026-10-29 15:29:59"), HOLIDAYS.session).number == 1
    assert review.cycle_at(evaluation, at("2026-10-29 15:30:00"), HOLIDAYS.session).number == 2


async def test_the_trader_s_start_begins_the_calendar_and_a_restart_goes_on_with_it(tmp_path: Path) -> None:
    """The trader started at 10:00 of 10-14, after its open, begins the evaluation version: its first
    cycle from the next trading day. Started again a week later, it keeps that first day."""
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    trader = TraderSettings(
        quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
        engine_url=f"http://127.0.0.1:{free_port()}",
        poll_seconds=0.01,
    )
    for n, moment in enumerate((at("10:00:00"), at("2026-10-21 06:00:00")), start=1):
        stop = asyncio.Event()

        def clock(moment: datetime = moment) -> datetime:
            return moment

        def started(n: int = n) -> bool:
            return (tmp_path / DB_NAME).exists() and len(rows(tmp_path, "runs")) == n

        task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=clock))
        await until(started)
        stop.set()
        await asyncio.wait_for(task, 5)
        found = evaluation_of(tmp_path)
        assert found is not None
        assert (found.first_day, found.begun_at) == (date(2026, 10, 15), at("10:00:00")), n
    assert len(rows(tmp_path, "evaluations")) == 1


# The frozen version.


def test_the_champion_s_cell_is_its_predictor_s_when_the_cost_map_s_top_turns_to_another(
    tmp_path: Path,
) -> None:
    """The champion's cell is the barrier's breiman, the cost map's top when it was fitted. Today the
    cost map's top is neyman, passing: the champion's cell is still breiman, and it trades off the cost
    map, without its figures (#472: the cost map's pass is no condition), while neyman, not the
    champion's, is not traded in its place. Back on the cost map, breiman trades with them."""
    first_champion(tmp_path)
    turned = choose(cost_map([cost_row("barrier", top=predictor("neyman", 0.72, "pass"))]))
    back = choose(cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))]))
    store = TraderStore(tmp_path / DB_NAME)
    try:
        under = under_champion(store, turned)
        again = under_champion(store, back)
    finally:
        store.close()

    [off] = under.picks
    assert (off.cell, off.threshold, off.low, off.p_star, under.reason, under.policy) == (
        BARRIER,
        0.6,
        None,
        None,
        None,
        1,
    )
    assert under.cells == (
        {
            "kind": "barrier",
            "role": "top",
            "predictor": "breiman",
            "threshold": 0.6,
            "traded": True,
            "why": None,
        },
    )
    [neyman] = [c for c in under.considered if c["predictor"] == "neyman"]
    assert (neyman["picked"], neyman["why"]) == (False, "not in policy 1")
    assert "barrier breiman (top: not on the cost map, θ 0.6)" in under.describe()
    [found] = again.picks
    assert (found.cell, found.low) == (BARRIER, 0.70) and again.cells[0]["traded"]

    trades = Day(tmp_path, desk=True, selection=turned).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", predictor="neyman")]),
            quote("10:00:02.5", 70_000, 70_100),
            Read("10:05:02.3", [Barrier("10:05:00")]),
            quote("10:05:02.5", 70_000, 70_100),
        ]
    )
    assert [t["predictor"] for t in trades] == ["breiman"], "no other cell takes the champion's place"
    [kept] = [r for r in rows(tmp_path, "selections")]
    assert (kept["reason"], kept["evaluation"], kept["cycle"]) == (None, 2, 1)
    assert '"traded": true' in kept["cells"]


def test_a_cell_whose_predictor_is_gone_trades_no_more_until_the_next_review(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The champion's cell is the barrier's breiman (its first cycle 10-14 to 11-10). On 10-13 the
    engine made barrier predictions of barrier-persist and none of breiman: at the 10-14 pick breiman
    is gone. Its predictions come back that morning, but it trades no more in the cycle, nor does
    another cell; the next cycle's first pick (11-11) finds it predicting, and it trades again."""
    first_champion(tmp_path)
    engine = PredictionStore(tmp_path / ENGINE_DB)
    Barrier("2026-10-13 10:00:00", predictor="barrier-persist").write(engine)
    engine.close()

    trades = Day(tmp_path, desk=True, gone=True, picks=(pick(BARRIER),)).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            Repick("10:30:00"),
            Read("10:35:02.3", [Barrier("10:35:00")]),
            quote("10:35:02.5", 70_000, 70_100),
            Repick("2026-11-11 08:30:00"),
            Read("2026-11-11 10:00:02.3", [Barrier("2026-11-11 10:00:00")]),
            quote("2026-11-11 10:00:02.5", 70_000, 70_100),
        ]
    )

    [traded] = trades
    assert (traded["bar_close"], traded["policy"]) == ("2026-11-11T10:00:00+09:00", 1)
    picks = rows(tmp_path, "selections")
    assert [(p["cycle"], p["reason"], p["picks"] == "[]") for p in picks] == [
        (1, NOT_IN_POLICY, True),
        (1, NOT_IN_POLICY, True),
        (2, None, False),
    ]
    assert ['"why": "gone"' in p["cells"] for p in picks] == [True, True, False]
    assert all(p["evaluation"] == 2 for p in picks)
    assert "policy 1's cell barrier breiman is gone" in caplog.text
    assert not [d for d in rows(tmp_path, "decisions") if d["bar_close"] < "2026-11-11"], "not read"


def test_a_predictor_is_gone_on_a_whole_day_alone(tmp_path: Path) -> None:
    """On 10-13 breiman and barrier-persist predicted; on 10-14 by 10:00:30 only barrier-persist's
    10:00 is stored (breiman, slower, not yet): a pick then does not find breiman gone. Nor does one
    without any prediction of the kind, nor one without the engine's database."""
    engine_db = tmp_path / ENGINE_DB
    assert gone(engine_db, SYMBOL, [BARRIER], at("10:00:30")) == set(), "no database"
    engine = PredictionStore(engine_db)
    assert gone(engine_db, SYMBOL, [BARRIER], at("10:00:30")) == set(), "no prediction of the kind"
    for shown in (
        Barrier("2026-10-13 10:00:00"),
        Barrier("2026-10-13 10:00:00", predictor="barrier-persist"),
        Barrier("10:00:00", predictor="barrier-persist"),
    ):
        shown.write(engine)
    engine.close()

    assert gone(engine_db, SYMBOL, [BARRIER], at("10:00:30")) == set()
    assert gone(engine_db, SYMBOL, [BARRIER], at("2026-10-15 08:30:00")) == {BARRIER}, (
        "10-14, whole, has none of it"
    )


def test_a_trade_keeps_its_model_version_and_the_evaluation_version_in_force(tmp_path: Path) -> None:
    """A trade on 10-14 before the evaluation version was begun is of the one before (1); begun after
    that day's close, the 10-15 trade is of version 2. Each keeps its prediction's model."""
    Day(tmp_path).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            quote("10:02:00", 70_300, 70_400),  # sold at the target
        ]
    )
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert review.begin(db, at("16:00:00"), CALENDAR.session).first_day == date(2026, 10, 15)
    Day(tmp_path, started_at=at("2026-10-15 09:00:00")).run(
        [
            Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
            quote("2026-10-15 10:00:02.5", 70_000, 70_100),
        ]
    )

    found = [(t["entry_at"][:10], t["model_version"], t["evaluation"]) for t in rows(tmp_path, "trades")]
    assert found == [("2026-10-14", "v1", 1), ("2026-10-15", "v1", 2)]


# The evaluation versions' evidence.


def test_the_gate_passed_under_the_version_before_is_kept_and_not_inherited(tmp_path: Path) -> None:
    """Under #408's rules policy 1 passes the gate's first checkpoint on its twenty days (``GOOD``).
    Then the evaluation version of #469 begins: those days and that pass stay in the store, but the
    gate on policy 1 has passed nothing and counts its history from the new first day; its next
    checkpoint is the trader's second (α_2), at the new version's twentieth day."""
    db = policies.connect(tmp_path / DB_NAME)
    try:
        policies.add(db, at("2026-08-31 22:31:00"), "밤 맞추기", [policy_cell(BARRIER)], [], {"grid": {}})
    finally:
        db.close()
    for day, net in zip(LONG[:20], GOOD, strict=True):
        day_ran(tmp_path, 1, day, net, champion=True)
    db = policies.connect(tmp_path / DB_NAME)
    try:
        [old] = promotion.check_gate(db, night(LONG[19]))  # the night's job before #469's deploy
        assert old.passed and promotion.gate_of(db, 1).passed
        review.begin(db, datetime.combine(LONG[20], time(6), KST), CALENDAR.session)
        gate = promotion.gate_of(db, 1)
        archived = promotion.checkpoints(db)
        records = promotion.records(db)
    finally:
        db.close()

    assert (gate.passed, gate.days, gate.history, gate.next, gate.next_day) == (False, 0, 0, 2, 20)
    assert gate.level == pytest.approx(promotion.checkpoint_level(2))
    assert [c.k for c in archived] == [1], "kept in the store"
    assert records == {}, "no day of the version before"
    assert len(rows(tmp_path, "policy_days")) == 20

    for day, net in zip(LONG[20:40], GOOD, strict=True):
        day_ran(tmp_path, 1, day, net, champion=True)
    done = judged_at(tmp_path, night(LONG[39]))
    [two] = done.checked
    assert (two.k, two.base, two.history_day, two.day, two.days) == (2, 1, 20, LONG[39], 20)
    assert two.low == pytest.approx(1_000 - 2.985660 * 290.19, abs=0.1)
    assert done.gate.passed_at == two
    assert "실전 관문 판정 2번째(모의 이력 20거래일째" in promotion.report(done)


def test_a_review_night_judges_once(tmp_path: Path) -> None:
    """Policies 2 and 3 alike beat the champion: on the review night the newer, 3, is promoted; run
    again that night, the job judges nothing more (policy 2, which may beat the new champion, waits
    for the next review)."""
    setup(tmp_path, [1_000.0] * 10, [1_000.0] * 10)

    first = judged_at(tmp_path, night(WEEKDAYS[19]))
    again = judged_at(tmp_path, night(WEEKDAYS[19]).replace(hour=23))
    between = judged_at(tmp_path, night(WEEKDAYS[20]))

    assert first.promoted == 3
    assert (again.deferred, again.promoted, again.review) == (REVIEWED, None, WEEKDAYS[19])
    assert (between.deferred, between.review) == (BETWEEN_REVIEWS, WEEKDAYS[39])


# What the Engine API gives.


def test_get_trader_gives_the_cycle_in_force(tmp_path: Path) -> None:
    """``GET /v1/trader``'s ``cycle``: null before the evaluation was begun; then the cycle in force
    on the engine's calendar, within the /v1 contract."""
    TraderStore(tmp_path / DB_NAME).close()
    assert read_trader(tmp_path / DB_NAME, at("2026-10-12 10:00:00"), HOLIDAYS.session)["cycle"] is None
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        review.begin(db, at("2026-10-01 06:00:00"), HOLIDAYS.session)

    during = read_trader(tmp_path / DB_NAME, at("2026-10-12 10:00:00"), HOLIDAYS.session)
    after = read_trader(tmp_path / DB_NAME, at("2026-10-29 16:00:00"), HOLIDAYS.session)

    assert during["cycle"] == {
        "evaluation": 2,
        "since": "2026-10-01",
        "number": 1,
        "start": "2026-10-01",
        "review": "2026-10-29",
        "day": 7,
        "days": 20,
    }
    assert after["cycle"] == {
        "evaluation": 2,
        "since": "2026-10-01",
        "number": 2,
        "start": "2026-10-30",
        "review": "2026-11-26",
        "day": 1,
        "days": 20,
    }, "after the review day's close: the next trading day's cycle"
    check("trader", during)
    check("trader", after)


def test_the_night_s_report_says_the_cycle_and_its_review_night(tmp_path: Path) -> None:
    """The report of a day names its cycle and its place in it; the review day's says that night is
    the review."""
    setup(tmp_path, [0.0] * 10)

    tenth = reported(tmp_path, f"{WEEKDAYS[9]} 22:40:00")
    twentieth = reported(tmp_path, f"{WEEKDAYS[19]} 22:40:00")

    assert (
        "검토 주기 (#469): 평가 버전 2 · 주기 1(2026-09-01부터, 검토 날 2026-09-28) · 그날 10일째 · "
        "챔피언은 검토 날 장이 끝난 뒤에만 바뀐다(판정은 그날 밤)" in tenth.splitlines()
    )
    assert "그날 20일째 · 그날 밤이 검토다" in twentieth
