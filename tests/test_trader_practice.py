"""The mock practice (#427, ``KRX_TRADER_MOCK_PRACTICE``): on a day no champion cell trades, the mock
book trades the cost map's practice cell a share at a time on KIS's mock server, its trades, orders
and fills kept as practice, its paper shadow a book of its own; the KIS checks and the loss limits
count them, no record, promotion or live gate does.

KIS is the fake one (``tests/test_trader_mock.py``). The example: the cost map fails the 4-minute
barrier's breiman (its hit rate's lower bound 0.60 under p* 0.63): no cell passes, the practice cell
is that one. The 10:00 barrier prediction (up_first 0.7 ≥ θ 0.6, target 70,250, stop 69,850, time
barrier 10:04) read at 10:00:02.3; the first quote after it asks 70,100: KIS fills the practice's one
share at 70,050 and its take-profit rests at 70,300; the practice shadow fills its share at 70,100 in
paper. The bid reaches 70,300 at 10:02: the shadow takes profit at the target (70,250), KIS fills the
take-profit at its limit (70,300) at 10:02:30. Every value is made up.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli, kis
from krx_quant.config import SettingsError
from krx_quant.consensus import FINAL
from krx_quant.fake_kis import BUY_SIDE, SELL_SIDE, FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.trader import MOCK, PAPER, SYMBOL, live, promotion, report
from krx_quant.trader.cells import Cell
from krx_quant.trader.desk import Desk
from krx_quant.trader.gap import day_gap, pairing
from krx_quant.trader.limits import RUNNING
from krx_quant.trader.marks import read_marks
from krx_quant.trader.paper import STOP_LOSS, TAKE_PROFIT, TIME, Rules
from krx_quant.trader.selection import NO_COST_MAP, NO_PASS, NO_POLICY, NOT_IN_POLICY, Selection, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DAILY_LIMIT, DB_NAME, Entry, Exit, TraderStore, read_days, read_trades
from tests.api_schema import check
from tests.test_trader_mock import TIMING, Fill, kis_mock, orders_of, rows, settings_of
from tests.test_trader_shadow import BOUGHT, DAY, TAKEN, TEN, MockDesk, Repicked
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    Barrier,
    Direction,
    Read,
    Tick,
    at,
    cost_map,
    cost_row,
    keep,
    policy_cell,
    predictor,
    quote,
)

__all__ = ["kis_mock"]  # the fixture, used here

# No cell passes: the barrier's breiman fails (0.60 under p* 0.63), the practice cell.
FAILING = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])
PASSING = cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])
OTHER = Cell("barrier_15m", "breiman")  # a cell of the champion the cost map does not pass


def practised() -> Selection:
    return choose(FAILING)


def shadows(state: Path) -> list[dict[str, Any]]:
    """The paper shadows' trades, oldest first: the champion policy's and the practice one's."""
    return [r for r in rows(state, "trades") if r["shadow"]]


def mock_picks(state: Path) -> list[dict[str, Any]]:
    """The mock book's picks, oldest first."""
    return [r for r in rows(state, "selections") if r["mode"] == MOCK]


def order_checks(state: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return db.execute("SELECT mode, day, verdict FROM order_checks ORDER BY day").fetchall()


# The setting


def test_the_practice_is_a_setting_of_the_mock_mode_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off by default; on with mock. With paper or live it is a configuration error, checked before a
    live trader falls back to paper: ``krx-quant trade`` exits 2 and keeps no run."""
    assert TraderSettings.from_env({}, None).mock_practice is False
    mock = {"KRX_TRADER_MODE": "mock"}
    assert TraderSettings.from_env(mock | {"KRX_TRADER_MOCK_PRACTICE": "on"}, None).mock_practice
    assert not TraderSettings.from_env(mock | {"KRX_TRADER_MOCK_PRACTICE": "OFF"}, None).mock_practice
    with pytest.raises(SettingsError, match="must be on or off"):
        TraderSettings.from_env(mock | {"KRX_TRADER_MOCK_PRACTICE": "yes"}, None)
    for mode in ("paper", "live"):
        with pytest.raises(SettingsError, match=f"KRX_TRADER_MODE=mock alone, not {mode}"):
            TraderSettings.from_env({"KRX_TRADER_MODE": mode, "KRX_TRADER_MOCK_PRACTICE": "on"}, None)
    with pytest.raises(SettingsError, match="KRX_TRADER_MODE=mock alone, not paper"):
        TraderSettings.from_env({"KRX_TRADER_MOCK_PRACTICE": "on"}, None)  # paper by default

    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_TRADER_MODE", "live")
    monkeypatch.setenv("KRX_TRADER_MOCK_PRACTICE", "on")
    assert cli.main(["trade"]) == 2
    assert "configuration error: KRX_TRADER_MOCK_PRACTICE=on is for KRX_TRADER_MODE=mock alone, not live" in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "state" / DB_NAME).exists(), "no run kept: it never ran as paper"

    store = TraderStore(tmp_path / DB_NAME)
    try:
        with pytest.raises(ValueError, match="mock mode alone"):
            Desk(store, Rules(fee_rate=FEE), CALENDAR.session, at("09:00:00"), PAPER, practice=True)
    finally:
        store.close()


# The practice cell


def test_the_practice_cell_has_the_greatest_lower_bound_less_p_star_of_the_cells_with_a_verdict() -> None:
    """``fail`` counts and ``few`` does not; a cell without a predictor or p*, one whose lower bound is
    not finite, one whose row has no barrier width are left out. One share at the default θ."""
    no_width = cost_row("barrier_30m", top=predictor("turing", 0.629, "fail"))
    no_width["width"] = 0  # its barrier is not sound
    found = choose(
        cost_map(
            [
                cost_row(
                    "direction",
                    consensus=predictor(FINAL, 0.629, "few"),  # the greatest, but few calls
                    top=predictor("turing", 0.60, "fail"),
                ),
                cost_row(
                    "barrier", top=predictor("breiman", 0.62, "fail"), baseline=predictor("ar", 0.5, "fail")
                ),
                cost_row("barrier_15m", top=predictor("cox", float("inf"), "fail")),
                no_width,
            ]
        )
    )

    assert (found.picks, found.reason) == ((), NO_PASS)
    practice = found.practice
    assert practice is not None
    assert (practice.kind, practice.role, practice.predictor) == ("barrier", "top", "breiman")
    assert practice.priority == pytest.approx(0.62 - 0.63)
    assert (practice.quantity, practice.practice, practice.threshold, practice.fraction) == (
        1,
        True,
        None,
        0.0,
    )
    assert practice.kelly is not None and practice.kelly < 0, "its Kelly at most 0: one share"

    tie = choose(
        cost_map(
            [
                cost_row("barrier_15m", top=predictor("cox", 0.60, "fail")),
                cost_row("barrier", top=predictor("breiman", 0.60, "fail")),
            ]
        )
    )
    assert tie.practice is not None and tie.practice.kind == "barrier", "of two alike, the kinds' order"
    assert choose(cost_map([cost_row("barrier", top=predictor("breiman", 0.6, "few"))])).practice is None


async def test_the_practice_is_picked_only_on_a_cost_map_and_a_champion_read_with_no_champion_cell(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """``no_pass`` and ``not_in_policy`` practise; ``no_cost_map``, ``no_policy`` and a day with a
    champion cell do not. The practice off, none does."""
    fake, _ = kis_mock
    store = TraderStore(tmp_path / DB_NAME)
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(
                session,
                settings_of(fake, tmp_path),
                lambda: at("09:00:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )

            def desk(practice: bool) -> Desk:
                return Desk(
                    store,
                    Rules(fee_rate=FEE),
                    CALENDAR.session,
                    at("09:00:00"),
                    MOCK,
                    broker=broker,
                    clock=lambda: at("09:00:00"),
                    practice=practice,
                )

            on = desk(practice=True)
            cell = Cell("barrier", "breiman")
            on.select(practised(), at("09:00:00"))  # before the first champion: no_pass
            assert on.champion.cells == [cell] and on.shadow is not None and on.shadow.cells == []
            assert on.practice_shadow is not None and on.practice_shadow.cells == [cell]
            assert on.champion.practice_state is not None
            assert (
                on.champion.practice_state["active"] and on.champion.practice_state["cell"]["quantity"] == 1
            )
            no_map = Selection((), reason=NO_COST_MAP, detail="starting", practice=practised().practice)
            on.select(no_map, at("09:01:00"))
            assert on.champion.cells == [] and on.practice_shadow.cells == []
            assert on.champion.practice_state == {"enabled": True, "active": False, "cell": None}
            on.select(choose(PASSING), at("09:02:00"))
            assert on.champion.cells == [cell] and not on.champion.practice_state["active"]
            keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(OTHER))
            on.select(choose(PASSING), at("09:03:00"))  # the champion's cell is not the one passing
            assert on.champion.cells == [cell] and on.champion.practice_state["active"]
            last = mock_picks(tmp_path)[-1]
            assert (last["mode"], last["reason"], last["practice"], last["policy_id"]) == (
                MOCK,
                NOT_IN_POLICY,
                1,
                1,
            )
            with store.db:  # the champion cannot be read
                store.db.execute("UPDATE policies SET cells = 'broken' WHERE id = 1")
            on.select(practised(), at("09:04:00"))
            assert on.champion.cells == [] and not on.champion.practice_state["active"]
            assert mock_picks(tmp_path)[-1]["reason"] == NO_POLICY
            await on.close()

            off = desk(practice=False)
            off.select(practised(), at("09:05:00"))
            assert off.champion.cells == [] and off.practice_shadow is None
            assert off.champion.practice_state == {"enabled": False, "active": False, "cell": None}
            await off.close()
    finally:
        store.close()


# A practice day


async def test_a_day_without_a_champion_cell_practises_a_share_on_kis_kept_as_practice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    assert keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(OTHER)) == 1  # the champion
    desk = MockDesk(tmp_path, fake, account, selection=practised(), practice=True)

    await desk.run([TEN, BOUGHT, TAKEN, Fill("10:02:30")])

    # The trader's trade: KIS's, of the practice cell, one share, of no policy.
    [mine] = desk.trades()
    assert (mine["mode"], mine["champion"], mine["shadow"], mine["practice"], mine["policy"]) == (
        MOCK,
        True,
        False,
        True,
        None,
    )
    assert (mine["kind"], mine["predictor"], mine["role"]) == ("barrier", "breiman", "top")
    assert mine["entry"] == {"at": "2026-10-14T10:00:02.500000+09:00", "price": 70_050, "quantity": 1}
    assert mine["exit"] == {"at": "2026-10-14T10:02:30+09:00", "price": 70_300, "reason": TAKE_PROFIT}
    assert (mine["threshold"], mine["fraction"], mine["p_star"], mine["precision_low"]) == (
        0.6,
        None,
        pytest.approx(0.63),
        0.60,
    )
    assert orders_of(mine) == [
        ("buy", "entry", 70_100, 1, "filled", 1),
        ("sell", "take_profit", 70_300, 1, "filled", 1),
    ]
    assert [o.number for o in account.placed(BUY_SIDE) + account.placed(SELL_SIDE)] == [
        o["broker_id"] for o in mine["orders"]
    ], "the one KIS book's orders, every one in the ledger"
    assert account.holdings[SYMBOL] == 0 and desk.status == RUNNING
    check("trader_trades", {"trades": desk.trades()})
    # Its paper shadow: the practice shadow's, never the champion policy's shadow.
    [shadow] = shadows(tmp_path)
    assert (shadow["mode"], shadow["practice"], shadow["champion"], shadow["policy_id"]) == (
        PAPER,
        1,
        0,
        None,
    )
    assert (shadow["entry_price"], shadow["quantity"], shadow["exit_price"]) == (70_100, 1, 70_250)
    assert desk.trades(policy=1) == [], "no trade of the champion policy"
    # The pairs: the practice trade against the practice shadow's.
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        gap = day_gap(db, DAY)
        kept = promotion.records(db)
    assert gap is not None and (gap["mock"]["trades"], gap["shadow"]["trades"]) == (1, 1)
    [pair] = gap["pairs"]
    assert (pair["practice"], pair["entry_ticks"], pair["exit_ticks"]) == (
        True,
        pytest.approx(-0.5),
        pytest.approx(0.5),
    )
    [day] = [d for d in read_days(tmp_path / DB_NAME, DAY, DAY) if d["mode"] == MOCK]
    assert day["gap"] == gap
    check(
        "trader_days",
        {"from": "2026-10-14", "to": "2026-10-14", "days": read_days(tmp_path / DB_NAME, DAY, DAY)},
    )
    # The picks: the mock book's and the practice shadow's are practice, the champion's shadow's not.
    picks = [
        (s["mode"], s["practice"], s["reason"], s["policy_id"], json.loads(s["picks"]))
        for s in rows(tmp_path, "selections")
    ]
    assert [p[:4] for p in picks] == [(MOCK, 1, NO_PASS, 1), (PAPER, 0, NO_PASS, 1), (PAPER, 1, NO_PASS, 1)]
    assert [(p["kind"], p["practice"], p["quantity"]) for p in picks[0][4]] == [("barrier", True, 1)]
    assert picks[1][4] == []
    # The record: the champion ran that day and traded nothing; the practice counts in none.
    [champion_day] = kept[1].days
    assert (champion_day.day, champion_day.trades, champion_day.net, champion_day.champion) == (
        DAY,
        0,
        0.0,
        True,
    )
    store = TraderStore(tmp_path / DB_NAME)
    try:
        assert store.paper_account() == 0.0, "the practice shadow's net is no paper account's"
    finally:
        store.close()
    # GET /v1/trader: the practice on, practising, its cell.
    state = desk.state_now
    assert state is not None and state["practice"] is not None
    assert (state["practice"]["enabled"], state["practice"]["active"]) == (True, True)
    assert (state["practice"]["cell"]["kind"], state["practice"]["cell"]["quantity"]) == ("barrier", 1)
    check("trader", state)
    # The chart marks it as the trader's, practice.
    marks = read_marks(tmp_path / DB_NAME, SYMBOL, None, 10)
    assert [(m["side"], m["practice"], m["policy"]) for m in marks] == [
        ("sell", True, None),
        ("buy", True, None),
    ]
    check("trader_marks", {"symbol": SYMBOL, "marks": marks})
    # The night's report says it apart, with no price.
    text = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR))
    assert "매매 없음." in text, "no trade of the champion's"
    assert "연습 매매 1건(KIS 모의" in text
    assert "연습 칸(KIS 모의, KRX_TRADER_MOCK_PRACTICE): barrier breiman(top)" in text
    assert "매매할 칸 없음: 비용 지도에서 시장가 왕복으로 통과한 칸이 없다(no_pass)" in text
    assert "- 10:00 barrier breiman(연습): 진입 -0.50틱 · 청산 +0.50틱 · 1주 ↔ 1주" in text
    assert "읽은 예측(연습 칸 고르기의 챔피언 장부): 읽은 예측 1 · 산 것 1" in text
    assert "70,050" not in text and "70050" not in text, "no price"


async def test_the_kis_checks_count_the_practice_and_the_records_and_the_gate_do_not(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """After the session the day's orders, the practice's, are compared with KIS's: the same. The live
    mode's execution checks count the day, its round trip and its legs against the practice shadow;
    the champion's record, its paper account and the live gate hold nothing of it."""
    fake, account = kis_mock
    keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(OTHER))
    desk = MockDesk(
        tmp_path,
        fake,
        account,
        selection=practised(),
        practice=True,
        stop_when=lambda: bool(order_checks(tmp_path)),
    )

    await desk.run([TEN, BOUGHT, TAKEN, Fill("10:02:30"), Tick("15:31:00")])

    assert order_checks(tmp_path) == [(MOCK, "2026-10-14", "same")]
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        ready = live.readiness(db, CALENDAR.session, date(2026, 10, 15))
        found = {c.name: c for c in ready.checks}
        assert (found["mock_days"].value, found["mock_trips"].value) == (1.0, 1.0)
        assert found["order_checks"].passed and found["order_failed"].passed
        assert found["unresolved"].passed and found["flat"].passed and found["reconcile"].passed
        legs = live.legs(db, MOCK, ready.period, live.MOCK_CORRECTION)
        assert legs.values == pytest.approx((0.0, 0.0)), "its two legs, corrected"
        assert legs.alone == 0
        assert found["gap"].passed and "다리 2개" in found["gap"].said
        gate = promotion.gate_of(db, 1)
        [champion_day] = promotion.records(db)[1].days
    assert (champion_day.net, champion_day.trades, champion_day.ticks) == (0.0, 0, 0.0)
    assert (gate.policy, gate.days, gate.mean) == (1, 1, 0.0), "the gate's record holds no practice"


# The practice ends


@pytest.mark.parametrize("practice", [True, False])
async def test_a_restart_with_a_practice_position_sells_it_and_the_reconcile_holds_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, practice: bool
) -> None:
    """The trader stops holding the practice's share (its take-profit resting on KIS) and starts again
    at 10:05, past its time barrier, with a champion cell back, the practice on or off: the account is
    as the ledger has it (the share is the ledger's), and the share is sold at the first quote's bid,
    as is the practice shadow's in paper. The champion's cell then buys as ever."""
    fake, account = kis_mock
    keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(BARRIER))
    await MockDesk(tmp_path, fake, account, selection=practised(), practice=True).run([TEN, BOUGHT])
    assert [(t["practice"], t["exit"]) for t in read_trades(tmp_path / DB_NAME, None, 10)] == [(True, None)]

    again = MockDesk(
        tmp_path,
        fake,
        account,
        started_at=at("10:05:00"),
        selection=choose(PASSING),
        practice=practice,
    )
    await again.run(
        [
            quote("10:05:00.5", 70_000, 70_100),
            Read("10:08:02.3", [Barrier("10:08:00")]),
            quote("10:08:02.5", 70_000, 70_100),
        ]
    )

    reconciled = rows(tmp_path, "reconciles")
    assert [r["status"] for r in reconciled] == ["clean", "clean"], "the practice's share is the ledger's"
    assert not [c for c in rows(tmp_path, "controls") if c["source"] == "reconcile"]
    assert again.status == RUNNING
    champion, practised_ = read_trades(tmp_path / DB_NAME, None, 10)
    assert (practised_["practice"], practised_["entry"]["quantity"], practised_["exit"]["reason"]) == (
        True,
        1,
        TIME,
    )
    assert (champion["practice"], champion["policy"], champion["entry"]["quantity"]) == (False, 1, 14)
    [shadowed, _] = shadows(tmp_path)
    assert (shadowed["practice"], shadowed["exit_reason"]) == (1, TIME), "the practice shadow sold it"
    assert again.state_now is not None
    assert again.state_now["practice"] == {"enabled": practice, "active": False, "cell": None}


async def test_a_champion_cell_back_stops_new_practice_and_the_practice_position_sells_by_its_rules(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The practice of the direction's consensus buys its share at 10:00. At 10:01 a pick passes the
    barrier's breiman: the practice is picked no more, and the barrier's rise of 10:01 finds the
    account's one position taken (``holding``) while the champion's shadow buys it in paper. The
    practice share is sold at its time barrier (10:04); the barrier's rise of 10:08 buys on KIS."""
    fake, account = kis_mock
    start = choose(
        cost_map(
            [
                cost_row("direction", consensus=predictor(FINAL, 0.62, "fail")),
                cost_row("barrier", top=predictor("breiman", 0.55, "fail")),
            ]
        )
    )
    assert start.practice is not None and start.practice.predictor == FINAL
    desk = MockDesk(tmp_path, fake, account, selection=start, practice=True)

    await desk.run(
        [
            Read("10:00:02.3", [Direction("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            Repicked("10:01:00", choose(PASSING)),
            Read("10:01:02.3", [Barrier("10:01:00")]),
            quote("10:01:02.5", 70_000, 70_100),
            quote("10:03:59", 70_000, 70_100),
            quote("10:04:00.5", 70_000, 70_100),
            quote("10:04:59", 70_000, 70_100),  # the champion's shadow sells its barrier trade at 10:05
            quote("10:05:00.5", 70_000, 70_100),
            Read("10:08:02.3", [Barrier("10:08:00")]),
            quote("10:08:02.5", 70_000, 70_100),
        ]
    )

    champion, practised_ = desk.trades()
    assert (practised_["practice"], practised_["kind"], practised_["entry"]["quantity"]) == (
        True,
        "direction",
        1,
    )
    assert practised_["exit"]["reason"] == TIME
    assert (champion["practice"], champion["bar_close"], champion["entry"]["quantity"]) == (
        False,
        "2026-10-14T10:08:00+09:00",
        14,
    )
    skipped = [
        d["reason"]
        for d in rows(tmp_path, "decisions")
        if d["mode"] == MOCK and d["bar_close"].endswith("10:01:00+09:00")
    ]
    assert skipped == ["holding"], "one position at a time, account-wide"
    paper = [(t["practice"], t["kind"], t["quantity"]) for t in shadows(tmp_path)]
    assert paper == [(1, "direction", 1), (0, "barrier", 14), (0, "barrier", 14)]
    assert desk.status == RUNNING


# What survives: the write-ahead buy, the lost answer, the cancel


async def test_a_practice_buy_whose_answer_was_lost_is_adopted_as_practice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.lose_answers = 1
    await MockDesk(tmp_path, fake, account, selection=practised(), practice=True).run([TEN, BOUGHT])

    [trade] = read_trades(tmp_path / DB_NAME, None, 10)
    assert (trade["practice"], trade["policy"], trade["entry"]["quantity"]) == (True, None, 1)
    [kept] = rows(tmp_path, "unfilled_buys")
    assert (kept["status"], kept["practice"], json.loads(kept["entry"])["practice"]) == ("filled", 1, True)


async def test_a_practice_buy_kis_took_before_a_stop_is_recovered_as_practice_after_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The trader stops while it reads the practice buy's fills; started again with the practice off,
    it takes the buy off and the share that filled opens a practice trade."""
    fake, account = kis_mock
    account.fills, account.fills_late = False, True
    held = lambda: any(r["status"] == "open" for r in rows(tmp_path, "unfilled_buys"))  # noqa: E731
    first = MockDesk(
        tmp_path,
        fake,
        account,
        selection=practised(),
        practice=True,
        timing=replace(TIMING, wait=30.0),
        stop_when=held,
    )
    await first.run([TEN, BOUGHT])
    assert read_trades(tmp_path / DB_NAME, None, 10) == []

    await MockDesk(tmp_path, fake, account, started_at=at("10:01:00")).run([Tick("10:01:00.5")])

    [trade] = read_trades(tmp_path / DB_NAME, None, 10)
    assert (trade["practice"], trade["entry"]["quantity"]) == (True, 1)
    assert [r["practice"] for r in rows(tmp_path, "unfilled_buys")] == [1]


async def test_a_practice_buy_of_which_nothing_filled_is_kept_as_practice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.fills, account.fills_late = False, False
    await MockDesk(tmp_path, fake, account, selection=practised(), practice=True).run([TEN, BOUGHT])

    assert read_trades(tmp_path / DB_NAME, None, 10) == []
    [kept] = rows(tmp_path, "unfilled_buys")
    assert (kept["status"], kept["practice"], kept["quantity"]) == ("cancelled", 1, 1)


# The loss limits


async def test_the_loss_limits_count_the_champion_and_the_practice_kis_net_together(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A capital of 80,000 won: a day's limit of 8,000. The champion's KIS trade lost 5,000 won that
    morning; the practice's share bought at 70,050 is marked at a bid of 66,000: (66,000 − 70,050) less
    the buy's commission (10.5075) and the sale's commission and tax at the bid (66,000 × 0.215% =
    141.9): −4,202.4 won. Together −9,202.4: the day's limit, though neither alone reaches it. The
    paper shadows count nowhere, and a start with the practice off holds the day's loss still."""
    fake, account = kis_mock
    rules = Rules(capital=80_000, fee_rate=FEE)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        lost = Entry(
            mode=MOCK,
            symbol=SYMBOL,
            kind="barrier",
            predictor="breiman",
            prediction_id=0,
            bar_close=at("09:30:00"),
            probability=0.7,
            threshold=0.6,
            anchor=70_050,
            tick=100,
            width_ticks=2,
            target=70_250,
            stop=69_850,
            time_barrier=at("09:34:00"),
            decided_at=at("09:30:02"),
            entry_at=at("09:30:02"),
            entry_price=70_000,
            quantity=1,
            buy_fee=0.0,
            quote_at=at("09:30:02"),
        )
        trade = store.enter(0, lost)
        store.exit(trade, Exit(at("09:34:00"), 65_000, STOP_LOSS, 0.0, 0.0, at("09:34:00")))
    finally:
        store.close()
    desk = MockDesk(tmp_path, fake, account, rules=rules, selection=practised(), practice=True)

    await desk.run([TEN, BOUGHT, quote("10:01:00", 66_000, 66_100)])

    [halt] = rows(tmp_path, "halts")
    assert (halt["mode"], halt["kind"]) == (MOCK, DAILY_LIMIT)
    assert halt["realized"] == pytest.approx(-5_000), "the champion's KIS trade alone: no shadow"
    assert halt["unrealized"] == pytest.approx(-4_202.4075)
    assert halt["pnl"] == pytest.approx(-9_202.4075)
    assert desk.status == DAILY_LIMIT
    practised_, _ = read_trades(tmp_path / DB_NAME, None, 10)
    assert practised_["practice"] and practised_["exit"] is not None
    net = -5_000 + practised_["net"]

    off = MockDesk(tmp_path, fake, account, rules=rules, started_at=at("10:30:00"), practice=False)
    await off.run([Tick("10:30:01")])

    assert off.status == DAILY_LIMIT, "the practice off, the day's loss holds"
    assert off.state_now is not None and off.state_now["today"]["realized"] == pytest.approx(net)


# The pairs


def test_a_pair_is_of_one_book_and_every_trade_is_in_exactly_one(tmp_path: Path) -> None:
    """A practice trade pairs with the practice shadow's trade of its prediction, never with the
    champion policy's shadow; two KIS trades of one prediction pair with none, each listed alone, and
    the live gap check says the KIS trades no leg measures."""
    store = TraderStore(tmp_path / DB_NAME)

    def made(
        prediction: int, mode: str, *, champion: bool, shadow: bool, practice: bool, price: float
    ) -> None:
        entry = Entry(
            mode=mode,
            symbol=SYMBOL,
            kind="barrier",
            predictor="breiman",
            prediction_id=prediction,
            bar_close=at("10:00:00"),
            probability=0.7,
            threshold=0.6,
            anchor=70_050,
            tick=100,
            width_ticks=2,
            target=70_250,
            stop=69_850,
            time_barrier=at("10:04:00"),
            decided_at=at("10:00:02"),
            entry_at=at("10:00:02.5"),
            entry_price=price,
            quantity=1,
            buy_fee=0.0,
            quote_at=at("10:00:02.5"),
            champion=champion,
            shadow=shadow,
            practice=practice,
        )
        trade = store.enter(0, entry)
        store.exit(trade, Exit(at("10:04:00"), price, TIME, 0.0, 0.0, at("10:04:00")))

    try:
        made(1, MOCK, champion=True, shadow=False, practice=True, price=70_050)
        made(1, PAPER, champion=False, shadow=True, practice=False, price=70_300)  # the champion's shadow
        made(1, PAPER, champion=False, shadow=True, practice=True, price=70_100)
        made(2, MOCK, champion=True, shadow=False, practice=False, price=70_050)
        made(2, MOCK, champion=True, shadow=False, practice=False, price=70_050)  # twice: pairs none
        made(2, PAPER, champion=False, shadow=True, practice=False, price=70_100)
        found = pairing(store.db, MOCK, [DAY])
        legs = live.legs(store.db, MOCK, [DAY], 0.0)
        store.db.row_factory = sqlite3.Row
        gap = day_gap(store.db, DAY)
    finally:
        store.close()

    [(kis, paper)] = found.pairs
    assert (kis["practice"], paper["practice"], kis["prediction_id"], paper["entry_price"]) == (
        1,
        1,
        1,
        70_100,
    )
    assert len(found.alone) == 4 and found.kis_alone == 2
    assert [(m is None, s is None) for m, s in found.alone] == [
        (True, False),  # the champion's shadow of prediction 1: no KIS trade of its book
        (False, True),
        (False, True),
        (True, False),
    ]
    assert legs.alone == 2 and legs.values == pytest.approx((-0.5, 0.5))
    said = live.gap_check("gap", legs, corrected=False).said
    assert "그림자와 짝이 없어 재지 않은 KIS 매매 2건" in said
    assert gap is not None and len(gap["pairs"]) == 5, "every trade in exactly one pair"
    assert sum((p["mock"] is not None) + (p["shadow"] is not None) for p in gap["pairs"]) == 6
