"""The mock practice (#427, ``KRX_TRADER_MOCK_PRACTICE``): on a day no champion cell trades, the mock
book trades the practice cell a share at a time on KIS's mock server, its trades, orders and fills
kept as practice, with no paper shadow since #548 (a practice shadow's trade left open from before
is sold by its barriers); the KIS checks and the risk rules count them, no record, promotion, live
gate nor, since #533, loss limit does. The cell is the one kept for the
review cycle (#474, ``tests/test_trader_practice_cycle.py``): here kept as the nightly job keeps it
(``practise``).

KIS is the fake one (``tests/test_trader_mock.py``). The example: the cost map fails the 4-minute
barrier's breiman (its hit rate's lower bound 0.60 under p* 0.63): no cell passes, and the practice
cell kept for the cycle is that one, at θ 0.6. The 10:00 barrier prediction (up_first 0.7 ≥ θ 0.6,
target 70,250, stop 69,850, time barrier 10:04) read at 10:00:02.3; the first quote after it asks
70,100: KIS fills the practice's one share at 70,050 and its take-profit rests at 70,300. The bid
reaches 70,300 at 10:02: KIS fills the take-profit at its limit (70,300) at 10:02:30. Every value is
made up.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli, kis
from krx_quant.config import SettingsError
from krx_quant.consensus import FINAL
from krx_quant.fake_kis import BUY_SIDE, SELL_SIDE, FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.trader import MOCK, PAPER, SYMBOL, limits, live, practice, promotion, report
from krx_quant.trader import policy as policies
from krx_quant.trader.book import BUY_LIMIT, COOLING_DOWN, STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.cells import Cell
from krx_quant.trader.desk import Desk
from krx_quant.trader.gap import day_gap, pairing
from krx_quant.trader.limits import RUNNING
from krx_quant.trader.marks import read_marks
from krx_quant.trader.selection import (
    NO_CHAMPION,
    NO_COST_MAP,
    NO_PASS,
    NO_POLICY,
    NOT_IN_POLICY,
    Selection,
    choose,
)
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import (
    CUMULATIVE_LIMIT,
    DAILY_LIMIT,
    DB_NAME,
    REARM,
    Entry,
    Exit,
    TraderStore,
    read_days,
    read_trades,
)
from tests.api_schema import check
from tests.kis_doubles import TIMING, Fill, kis_mock, orders_of, rows, settings_of
from tests.test_trader_shadow import BOUGHT, DAY, TAKEN, TEN, MockDesk, Repicked
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    Barrier,
    Direction,
    Read,
    Tick,
    at,
    cost_map,
    cost_row,
    keep,
    policy_cell,
    practise,
    predictor,
    quote,
    whole_rules,
)

__all__ = ["kis_mock"]  # the fixture, used here

# No cell passes: the barrier's breiman fails (0.60 under p* 0.63), the practice cell.
FAILING = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])
PASSING = cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])


def practised() -> Selection:
    return choose(FAILING)


def crowned(state: Path, made: datetime, *cells: policies.PolicyCell) -> int:
    """A policy of ``cells`` kept at ``made`` and crowned the champion at once (#472: a champion's cell
    trades whatever the cost map's verdict, so a champion cell comes back with a champion). Its
    version."""
    version = keep(state, made, *cells)
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        assert policies.crown(db, made, version, "test", {})
    return version


def frozen_here(state: Path, threshold: float = 0.6) -> practice.Frozen:
    """FAILING's barrier breiman kept as the practice cell of the first cycle (#474), at ``threshold``."""
    return practise(state, practised().practisable[0], threshold)


def shadows(state: Path) -> list[dict[str, Any]]:
    """The paper shadows' trades, oldest first: the champion policy's (and a practice one's left from
    before #548)."""
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
            Desk(store, whole_rules(), CALENDAR.session, at("09:00:00"), PAPER, practice=True)
    finally:
        store.close()


# The practice cell


def test_the_cells_that_may_be_practised_are_those_with_a_verdict_and_a_sound_row() -> None:
    """``fail`` counts and ``few`` does not; a cell without a predictor or p*, one whose lower bound is
    not finite, one whose row has no barrier width are left out. Each one share, θ to be frozen (#474),
    in the picks' order: the greatest lower bound less p* first."""
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
    assert [(p.kind, p.role, p.predictor) for p in found.practisable] == [
        ("barrier", "top", "breiman"),
        ("direction", "top", "turing"),
        ("barrier", "baseline", "ar"),
    ]
    first = found.practisable[0]
    assert first.priority == pytest.approx(0.62 - 0.63)
    assert (first.quantity, first.practice, first.threshold) == (1, True, None)
    assert first.kelly is not None and first.kelly < 0, "its Kelly at most 0: one share"

    tie = choose(
        cost_map(
            [
                cost_row("barrier_15m", top=predictor("cox", 0.60, "fail")),
                cost_row("barrier", top=predictor("breiman", 0.60, "fail")),
            ]
        )
    )
    assert [p.kind for p in tie.practisable] == ["barrier", "barrier_15m"], "of two alike, the kinds' order"
    assert choose(cost_map([cost_row("barrier", top=predictor("breiman", 0.6, "few"))])).practisable == ()


async def test_the_practice_is_picked_only_on_a_cost_map_and_a_champion_read_with_no_champion_cell(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """``no_pass``, ``not_in_policy`` and ``no_champion`` (#469: a cell passing before the first
    champion trades no more) practise the cell kept for the cycle, at its θ; ``no_cost_map``,
    ``no_policy`` and a day with a champion cell do not, each said in ``practice.why``. The practice
    off, none does."""
    fake, _ = kis_mock
    kept = frozen_here(tmp_path, threshold=0.7)
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
                    whole_rules(),
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
            assert on.practice_shadow is None, "the practice has no shadow (#548)"
            state = on.champion.practice_state
            assert state is not None and state["active"] and state["why"] is None
            assert (state["cell"]["quantity"], state["cell"]["threshold"]) == (1, 0.7), "the cycle's θ"
            assert state["frozen"] == kept.as_json()
            assert (state["frozen"]["cycle"], state["frozen"]["threshold"]) == (1, 0.7)
            no_map = Selection((), reason=NO_COST_MAP, detail="starting")
            on.select(no_map, at("09:01:00"))
            assert on.champion.cells == []
            assert on.champion.practice_state == {
                "enabled": True,
                "active": False,
                "cell": None,
                "frozen": kept.as_json(),
                "why": NO_COST_MAP,
            }
            on.select(choose(PASSING), at("09:02:00"))  # passing, but no champion yet (#469)
            assert on.champion.cells == [cell] and on.champion.practice_state["active"]
            assert on.champion.practice_state["cell"]["quantity"] == 1
            assert mock_picks(tmp_path)[-1]["reason"] == NO_CHAMPION
            keep(tmp_path, at("2026-10-13 22:31:00"))  # an empty champion (#472: its cells trade)
            on.select(choose(PASSING), at("09:03:00"))  # the champion has not the one passing
            assert on.champion.cells == [cell] and on.champion.practice_state["active"]
            last = mock_picks(tmp_path)[-1]
            assert (last["mode"], last["reason"], last["practice"], last["policy_id"]) == (
                MOCK,
                NOT_IN_POLICY,
                1,
                1,
            )
            keep(tmp_path, at("2026-10-13 22:32:00"), policy_cell(BARRIER))
            assert policies.crown(store.db, at("2026-10-13 22:33:00"), 2, "test", {})
            on.select(choose(PASSING), at("09:03:30"))  # a champion cell: no practice
            assert on.champion.cells == [cell] and not on.champion.practice_state["active"]
            assert on.champion.practice_state["cell"] is None
            assert on.champion.practice_state["why"] == practice.CHAMPION
            with store.db:  # the champion cannot be read
                store.db.execute("UPDATE policies SET cells = 'broken' WHERE id = 2")
            on.select(practised(), at("09:04:00"))
            assert on.champion.cells == [] and not on.champion.practice_state["active"]
            assert on.champion.practice_state["why"] == NO_POLICY
            assert mock_picks(tmp_path)[-1]["reason"] == NO_POLICY
            await on.close()

            off = desk(practice=False)
            off.select(practised(), at("09:05:00"))
            assert off.champion.cells == [] and off.practice_shadow is None
            assert off.champion.practice_state == {
                "enabled": False,
                "active": False,
                "cell": None,
                "frozen": None,
                "why": None,
            }
            await off.close()
    finally:
        store.close()


# A practice day


async def test_a_day_without_a_champion_cell_practises_a_share_on_kis_kept_as_practice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    assert keep(tmp_path, at("2026-10-12 22:31:00")) == 1  # the champion, empty
    frozen_here(tmp_path)
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
    # No paper shadow (#548): neither a practice one nor the champion policy's.
    assert shadows(tmp_path) == []
    assert desk.trades(policy=1) == [], "no trade of the champion policy"
    # No pair: the practice is left out of the KIS book against the shadow (#548).
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.row_factory = sqlite3.Row
        gap = day_gap(db, DAY)
        kept = promotion.records(db)
    assert gap is None
    [day] = [d for d in read_days(tmp_path / DB_NAME, DAY, DAY) if d["mode"] == MOCK]
    assert day["gap"] is None
    check(
        "trader_days",
        {"from": "2026-10-14", "to": "2026-10-14", "days": read_days(tmp_path / DB_NAME, DAY, DAY)},
    )
    # The picks: the mock book's is practice, the champion's shadow's not.
    picks = [
        (s["mode"], s["practice"], s["reason"], s["policy_id"], json.loads(s["picks"]))
        for s in rows(tmp_path, "selections")
    ]
    assert [p[:4] for p in picks] == [(MOCK, 1, NO_PASS, 1), (PAPER, 0, NO_PASS, 1)]
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
        assert store.paper_account() == 0.0, "the practice is no paper account's"
    finally:
        store.close()
    # GET /v1/trader: the practice on, practising, its cell.
    state = desk.state_now
    assert state is not None and state["practice"] is not None
    assert (state["practice"]["enabled"], state["practice"]["active"]) == (True, True)
    assert (state["practice"]["cell"]["kind"], state["practice"]["cell"]["quantity"]) == ("barrier", 1)
    assert (state["practice"]["frozen"]["predictor"], state["practice"]["frozen"]["cycle"]) == ("breiman", 1)
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
    assert (
        "연습 칸(KIS 모의, KRX_TRADER_MOCK_PRACTICE): barrier breiman(top): 검토 주기에 고정한 칸(#474)"
        " · θ 0.6" in text
    )
    assert "연습 칸 (#474): 주기 1에 고정 · barrier breiman(top) · θ 0.6(2026-10-13 22:40에 고름)" in text
    assert "매매할 칸 없음: 챔피언 정책 1에 칸이 없다(빈 정책 · 고르기의 기록은 no_pass이나" in text, (
        "the cause (#560): the empty champion, not the cost map's verdicts"
    )
    assert (
        "연습(KIS 모의) | 1건 | +2.5틱 | 1.6틱 162원 | +0.9틱 +88원 | 목표 100%, 손절 0%, 시간 0% | "
        "답 없음 1 | 0.630"
    ) in text.splitlines(), "the practice's row (#560): its share bought at 70,050 and sold at 70,300"
    assert "\n1. 10:00:02 → " not in text, "its trade's line folded apart (#560)"
    assert (
        report.practice_trades(report.read(tmp_path / DB_NAME, DAY, CALENDAR))
        .splitlines()[1]
        .startswith("1. 10:00:02 → 10:02:30 익절(take_profit) · barrier breiman(top)")
    )
    assert "KIS 모의와 그림자 장부" not in text, "no pair of the practice (#548)"
    assert "읽은 예측(연습 칸 고르기의 챔피언 장부): 읽은 예측 1 · 산 것 1" in text
    assert "70,050" not in text and "70050" not in text, "no price"


async def test_the_kis_checks_count_the_practice_and_the_records_and_the_gate_do_not(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """After the session the day's orders, the practice's, are compared with KIS's: the same. The live
    mode's execution checks count the day, its round trip and its legs against the paper rules' price
    at their decision quotes (#548: a quote of 70,000 / 70,100 before the prediction is read, its ask
    the buy's, and the target the take-profit's); the champion's record, its paper account and the
    live gate hold nothing of it."""
    fake, account = kis_mock
    keep(tmp_path, at("2026-10-12 22:31:00"))  # an empty champion
    frozen_here(tmp_path)
    desk = MockDesk(
        tmp_path,
        fake,
        account,
        selection=practised(),
        practice=True,
        stop_when=lambda: bool(order_checks(tmp_path)),
    )

    await desk.run(
        [quote("10:00:01", 70_000, 70_100), TEN, BOUGHT, TAKEN, Fill("10:02:30"), Tick("15:31:00")]
    )

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
        assert legs.total == 2
        assert found["gap"].passed and "KIS 다리 2개 가운데 결정 호가로 잰 2개(100%" in found["gap"].said
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
    as the ledger has it (the share is the ledger's), and the share is sold at the first quote's bid.
    The champion's cell then buys as ever, and its shadow beside it."""
    fake, account = kis_mock
    keep(tmp_path, at("2026-10-12 22:31:00"))  # an empty champion: no champion cell (#472)
    kept = frozen_here(tmp_path)
    await MockDesk(tmp_path, fake, account, selection=practised(), practice=True).run([TEN, BOUGHT])
    assert [(t["practice"], t["exit"]) for t in read_trades(tmp_path / DB_NAME, None, 10)] == [(True, None)]
    crowned(tmp_path, at("10:04:00"), policy_cell(BARRIER))  # a champion cell back

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
    assert (champion["practice"], champion["policy"], champion["entry"]["quantity"]) == (False, 2, 14)
    [shadowed] = shadows(tmp_path)
    assert (shadowed["practice"], shadowed["policy_id"]) == (0, 2), "the champion's shadow alone (#548)"
    assert again.state_now is not None
    assert again.state_now["practice"] == {
        "enabled": practice,
        "active": False,
        "cell": None,
        "frozen": kept.as_json() if practice else None,
        "why": "champion" if practice else None,
    }


async def test_a_practice_shadow_s_trade_left_open_from_before_is_sold_by_its_barriers(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A practice shadow's share bought at 10:00 before #548, open when the trader starts at 10:01 with
    the practice on: a book of it that picks nothing sells it at its time barrier (10:04), at the bid
    of the last quote before it; no new practice shadow buys nor picks."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.enter(
            0,
            Entry(
                mode=PAPER,
                symbol=SYMBOL,
                kind="barrier",
                predictor="breiman",
                prediction_id=1,
                bar_close=at("10:00:00"),
                probability=0.7,
                threshold=0.6,
                anchor=70_050,
                tick=100,
                width_ticks=2,
                target=70_250,
                stop=69_850,
                time_barrier=at("10:04:00"),
                decided_at=at("10:00:02.3"),
                entry_at=at("10:00:02.5"),
                entry_price=70_100,
                quantity=1,
                buy_fee=0.0,
                quote_at=at("10:00:02.5"),
                champion=False,
                shadow=True,
                practice=True,
            ),
        )
    finally:
        store.close()
    desk = MockDesk(tmp_path, fake, account, started_at=at("10:01:00"), selection=practised(), practice=True)

    await desk.run([quote("10:03:59", 70_000, 70_100), quote("10:04:00.5", 70_100, 70_200)])

    [left] = shadows(tmp_path)
    assert (left["practice"], left["exit_reason"], left["exit_price"]) == (1, TIME, 70_000)
    assert [s["mode"] for s in rows(tmp_path, "selections") if s["practice"]] == [MOCK], (
        "the mock book's pick alone: no practice shadow picks"
    )
    assert desk.status == RUNNING


async def test_a_champion_cell_back_stops_new_practice_and_the_practice_position_sells_by_its_rules(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The practice of the direction's consensus buys its share at 10:00. At 10:01 a pick passes the
    barrier's breiman: the practice is picked no more, and the barrier's rise of 10:01 finds the
    account's one position taken (``holding``) while the champion's shadow buys it in paper. The
    practice share is sold at its time barrier (10:04); the barrier's rise of 10:08 buys on KIS. The
    champion is empty until the barrier's breiman's policy is crowned for the 10:01 pick (#472: a
    champion's cell trades whatever the cost map's verdict, so only a champion with a cell stops the
    practice; #469: nothing but the practice trades before the first champion)."""
    fake, account = kis_mock
    keep(tmp_path, at("2026-10-13 22:31:00"))
    start = choose(
        cost_map(
            [
                cost_row("direction", consensus=predictor(FINAL, 0.62, "fail")),
                cost_row("barrier", top=predictor("breiman", 0.55, "fail")),
            ]
        )
    )
    assert start.practisable[0].predictor == FINAL
    practise(tmp_path, start.practisable[0])
    desk = MockDesk(tmp_path, fake, account, selection=start, practice=True)

    await desk.run(
        [
            Read("10:00:02.3", [Direction("10:00:00")]),
            quote("10:00:02.5", 70_000, 70_100),
            Repicked(
                "10:01:00",
                choose(PASSING),
                before=lambda: crowned(tmp_path, at("10:00:59"), policy_cell(BARRIER)),
            ),
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
    assert paper == [(0, "barrier", 14), (0, "barrier", 14)], "the practice has no shadow (#548)"
    assert desk.status == RUNNING


# What survives: the write-ahead buy, the lost answer, the cancel


async def test_a_practice_buy_whose_answer_was_lost_is_adopted_as_practice(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    account.lose_answers = 1
    frozen_here(tmp_path)
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
    frozen_here(tmp_path)
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
    frozen_here(tmp_path)
    await MockDesk(tmp_path, fake, account, selection=practised(), practice=True).run([TEN, BOUGHT])

    assert read_trades(tmp_path / DB_NAME, None, 10) == []
    [kept] = rows(tmp_path, "unfilled_buys")
    assert (kept["status"], kept["practice"], kept["quantity"]) == ("cancelled", 1, 1)


# The loss limits (#533: they count no practice trade)


def kept_trade(state: Path, entry: str, out: str, sold: float, *, practice: bool) -> None:
    """A KIS trade of the mock book kept as the trader keeps one: a share bought at 70,000 at ``entry``
    and sold at ``sold`` at ``out``, no costs; the practice's if ``practice``, else the champion's."""
    store = TraderStore(state / DB_NAME)
    try:
        trade = store.enter(
            0,
            Entry(
                mode=MOCK,
                symbol=SYMBOL,
                kind="barrier",
                predictor="breiman",
                prediction_id=0,
                bar_close=at(entry),
                probability=0.7,
                threshold=0.6,
                anchor=70_050,
                tick=100,
                width_ticks=2,
                target=70_250,
                stop=69_850,
                time_barrier=at(entry) + timedelta(minutes=4),
                decided_at=at(entry),
                entry_at=at(entry),
                entry_price=70_000,
                quantity=1,
                buy_fee=0.0,
                quote_at=at(entry),
                practice=practice,
            ),
        )
        store.exit(trade, Exit(at(out), sold, STOP_LOSS, 0.0, 0.0, at(out)))
    finally:
        store.close()


async def test_a_practice_loss_alone_reaches_neither_loss_limit(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A capital of 80,000 won, its daily limit narrowed to 1% (800 won) and its cumulative one to 2%
    (1,600). The practice's share bought at 70,050 is marked at a bid of 68,000: (68,000 − 70,050) less
    the buy's commission (10.5075) and the sale's commission and tax at the bid (68,000 × 0.215% =
    146.2): −2,206.7 won, past both limits, and its stop sells it. Before #533 that reached both; now
    no limit counts a practice trade: no halt, the trader runs on, and the 10:15 rise (15 minutes on,
    #598's spacing) buys the practice's next share. The state, the day's ledger and the report give the
    three P&Ls apart."""
    fake, account = kis_mock
    rules = whole_rules(capital=80_000, daily_loss_percent=1.0, cumulative_loss_percent=2.0)
    frozen_here(tmp_path)
    desk = MockDesk(tmp_path, fake, account, rules=rules, selection=practised(), practice=True)

    await desk.run(
        [
            TEN,
            BOUGHT,
            quote("10:01:00", 68_000, 68_100),
            Read("10:15:02.3", [Barrier("10:15:00")]),
            quote("10:15:02.5", 70_000, 70_100),
            quote("10:16:00", 70_000, 70_100),
        ]
    )

    assert rows(tmp_path, "halts") == [] and desk.status == RUNNING
    assert not [n for n in desk.notices if "손실 한도" in n]
    second, first = desk.trades()
    assert (first["practice"], first["exit"]["reason"], second["practice"], second["exit"]) == (
        True,
        STOP_LOSS,
        True,
        None,
    )
    assert first["net"] < -1_600, "past both limits"
    assert shadows(tmp_path) == [], "the practice has no shadow (#548)"
    # GET /v1/trader: the limits' count holds none of it; the practice's and the account's hold it all.
    state = desk.state_now
    assert state is not None
    open_ = state["position"]["unrealized"]
    assert state["position"]["practice"] and open_ == pytest.approx(-50 - 10.5075 - 70_000 * 0.00215)
    today, cumulative = state["today"], state["cumulative"]
    assert (today["realized"], today["unrealized"], today["net"], today["trades"], today["worst"]) == (
        0.0,
        0.0,
        0.0,
        0,
        0.0,
    )
    assert today["practice"] == {
        "realized": first["net"],
        "unrealized": open_,
        "net": pytest.approx(first["net"] + open_),
        "trades": 1,
    }
    assert today["account"] == today["practice"]
    assert (cumulative["net"], cumulative["worst"]) == (0.0, 0.0)
    assert (
        cumulative["practice"]
        == cumulative["account"]
        == {
            "realized": first["net"],
            "unrealized": open_,
            "net": pytest.approx(first["net"] + open_),
        }
    )
    assert state["limits"]["daily"]["used"] == state["limits"]["cumulative"]["used"] == 0.0
    check("trader", state)
    # GET /v1/trader/days: the day's ledger is the limits' count, the practice's beside it.
    [day] = [d for d in read_days(tmp_path / DB_NAME, DAY, DAY) if d["mode"] == MOCK]
    assert (day["net"], day["trades"], day["daily"]["worst"], day["cumulative"]["used"]) == (0.0, 0, 0.0, 0.0)
    assert day["practice"] == {
        "net": first["net"],
        "trades": 1,
        "cumulative": pytest.approx(first["net"] + open_),
    }
    assert day["account"] == day["practice"]
    check("trader_days", {"from": "2026-10-14", "to": "2026-10-14", "days": [day]})


async def test_on_a_day_of_the_practice_and_the_champion_the_limits_count_the_champion_s_alone(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A capital of 90,000 won: a day's limit of 9,000. The champion's KIS trade lost 5,000 won that
    morning; the practice's share bought at 70,050 is marked at a bid of 66,000: (66,000 − 70,050) less
    the buy's commission (10.5075) and the sale's commission and tax at the bid (66,000 × 0.215% =
    141.9): −4,202.4 won. Together −9,202.4, the day's limit
    before #533; now the limit counts −5,000, and the practice's stop sells its share. The account's
    P&L is both. At 10:30 a champion cell is back (crowned at 10:20) and buys its share on KIS at
    70,050; marked at 66,000 it is −4,202.4 too: −9,202.4 with the morning's, the day's limit, its halt
    holding the champion's P&L alone."""
    fake, account = kis_mock
    rules = whole_rules(capital=90_000)
    keep(tmp_path, at("2026-10-12 22:31:00"))  # an empty champion: no champion cell (#472)
    kept_trade(tmp_path, "09:30:00", "09:34:00", 65_000, practice=False)
    frozen_here(tmp_path)
    desk = MockDesk(tmp_path, fake, account, rules=rules, selection=practised(), practice=True)

    await desk.run([TEN, BOUGHT, quote("10:01:00", 66_000, 66_100)])

    assert rows(tmp_path, "halts") == [] and desk.status == RUNNING
    [practised_, _] = desk.trades()
    assert practised_["practice"] and practised_["exit"]["reason"] == STOP_LOSS
    practice_net = practised_["net"]
    assert practice_net < -4_000, "with the champion's −5,000, past the day's 9,000"
    state = desk.state_now
    assert state is not None
    today = state["today"]
    assert (today["realized"], today["trades"], today["net"]) == (-5_000, 1, -5_000)
    assert today["practice"] == {
        "realized": practice_net,
        "unrealized": 0.0,
        "net": practice_net,
        "trades": 1,
    }
    assert today["account"] == {
        "realized": -5_000 + practice_net,
        "unrealized": 0.0,
        "net": -5_000 + practice_net,
        "trades": 2,
    }
    assert state["limits"]["daily"]["used"] == pytest.approx(5_000 / 9_000)
    assert state["cash"] == pytest.approx(90_000 - 5_000 + practice_net), "the account's cash: both"
    [day] = [d for d in read_days(tmp_path / DB_NAME, DAY, DAY) if d["mode"] == MOCK]
    assert (day["net"], day["trades"]) == (-5_000, 1)
    assert day["practice"] == {"net": practice_net, "trades": 1, "cumulative": practice_net}
    assert day["account"] == {"net": -5_000 + practice_net, "trades": 2, "cumulative": -5_000 + practice_net}
    text = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR))
    said = (
        f"손익 셋 (#533): 계좌(KIS) {-5_000 + practice_net:+,.0f}원(매매 2건) = 한도가 세는 것 -5,000원(1건)"
        f" + 연습 {practice_net:+,.0f}원(1건)"
    )
    assert said in text
    assert (
        "연습 매매 1건(KIS 모의, 챔피언 칸이 없던 때 1주씩: 승격·관문·모의 기록·손실 한도에는 세지 않고"
        in text
    )
    assert f"· 연습 1건 {practice_net:+,.0f}원(한도에 세지 않음)" in text, "the last trading days"

    # The champion's cell back: its loss reaches the day's limit as ever.
    crowned(tmp_path, at("10:20:00"), policy_cell(BARRIER))
    again = MockDesk(
        tmp_path,
        fake,
        account,
        rules=rules,
        started_at=at("10:30:00"),
        selection=choose(PASSING),
        practice=True,
    )
    await again.run(
        [
            Read("10:30:02.3", [Barrier("10:30:00")]),
            quote("10:30:02.5", 70_000, 70_100),
            quote("10:31:00", 66_000, 66_100),
        ]
    )

    [halt] = rows(tmp_path, "halts")
    assert (halt["mode"], halt["kind"]) == (MOCK, DAILY_LIMIT)
    assert halt["realized"] == pytest.approx(-5_000), "the champion's KIS trade alone: no practice"
    assert halt["unrealized"] == pytest.approx(-4_202.4075)
    assert halt["pnl"] == pytest.approx(-9_202.4075)
    assert again.status == DAILY_LIMIT
    [notice] = [n for n in again.notices if "하루 손실 한도에 닿았습니다" in n]
    assert f"연습 매매의 손익 {practice_net:+,.0f}원은 한도에 세지 않습니다" in notice
    champion, _, _ = again.trades()
    assert (champion["practice"], champion["entry"]["price"]) == (False, 70_050)


def test_a_ledger_written_before_533_does_not_carry_its_practice_lows_into_the_limits_count(
    tmp_path: Path,
) -> None:
    """The deploy day: the trader before #533 rolled to 10-14 at 00:00 and wrote its ledger, whose lows
    counted the practice (here a practice trade sold at 09:30 for −2,000 won, its position marked at
    −2,500 before: the day's and the cumulative low −2,500), and left the practice columns null. The
    trader since #533 started at 10:00 takes no low of that row: the limits' count starts again from
    its own realized P&L (0), and its next write splits the row, the practice's beside it. A ledger it
    wrote itself keeps its lows (−300 here, a champion position's mark)."""
    kept_trade(tmp_path, "09:26:00", "09:30:00", 68_000, practice=True)
    limits_ = limits.Limits(1_000_000, 10, 20)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_run(at("2026-10-13 09:00:00"), MOCK, {})
        legacy = {"realized": -2_000.0, "trades": 1, "worst": -2_500.0, "cumulative": -2_000.0}
        legacy |= {"cumulative_worst": -2_500.0, "daily_limit": 100_000.0, "cumulative_limit": 200_000.0}
        store.put_day(MOCK, DAY, legacy | {"status": RUNNING, "halt": None}, at("09:30:01"))
        guard = limits.Guard(store, MOCK, limits_, at("10:00:00"), lambda _: None)
        assert (guard.worst, guard.cumulative_worst) == (0.0, 0.0), "no practice in the limits' lows"
        assert (guard.realized_day, guard.practice_day, guard.practice_trades_day) == (0.0, -2_000, 1)
        store.put_day(MOCK, DAY, guard.ledger(), at("10:00:01"))
        [day] = read_days(tmp_path / DB_NAME, DAY, DAY)
        assert (day["net"], day["daily"]["worst"], day["daily"]["used"]) == (0.0, 0.0, 0.0)
        assert (day["cumulative"]["worst"], day["cumulative"]["used"]) == (0.0, 0.0)
        assert day["practice"] == {"net": -2_000, "trades": 1, "cumulative": -2_000}

        mine = guard.ledger() | {"worst": -300.0, "cumulative_worst": -300.0}
        store.put_day(MOCK, DAY, mine, at("10:30:00"))
        again = limits.Guard(store, MOCK, limits_, at("10:31:00"), lambda _: None)
        assert (again.worst, again.cumulative_worst) == (-300.0, -300.0), "a split ledger's lows kept"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("changes", "after", "held"),
    [
        ({"daily_buys": 1}, [TAKEN, Fill("10:02:30")], BUY_LIMIT),
        ({"loss_streak": 1}, [quote("10:01:00", 68_000, 68_100)], COOLING_DOWN),
    ],
)
async def test_the_risk_rules_count_the_practice_as_ever(
    kis_mock: tuple[FakeKis, FakeAccount],
    tmp_path: Path,
    changes: dict[str, int],
    after: list[Any],
    held: str,
) -> None:
    """#533 leaves the risk rules as they were (spec #468 6: the practice trades by the champion's
    rules): with a day's buy orders narrowed to one, the practice's buy is the day's last; with a
    loss in a row starting a cooldown, the practice's stop loss starts one. Either holds the 10:08
    rise; the execution checks count the practice's round trip (as
    ``test_the_kis_checks_count_the_practice_and_the_records_and_the_gate_do_not`` shows of them all)."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    desk = MockDesk(
        tmp_path, fake, account, rules=whole_rules(**changes), selection=practised(), practice=True
    )

    await desk.run([TEN, BOUGHT, *after, Read("10:08:02.3", [Barrier("10:08:00")])])

    [trade] = desk.trades()
    assert trade["practice"] and trade["exit"] is not None
    skipped = [d["reason"] for d in rows(tmp_path, "decisions") if d["mode"] == MOCK][-1]
    assert skipped == held
    assert desk.state_now is not None and desk.state_now["risk"]["held"] == held
    assert desk.state_now["risk"]["buys"] == 1
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert live.trips(db, MOCK, [DAY]) == 1


async def test_a_cumulative_halt_the_practice_reached_before_533_holds_until_re_armed(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A capital of 80,000 won, its cumulative limit narrowed to 2% (1,600). On 10-13 a practice trade
    lost 2,000 won and, counted as it was before #533, reached the cumulative limit (``halts``). The
    trades counted again leave the practice out (the cumulative P&L the limit counts is 0), but the
    halt kept holds as any (a halt is kept and no later count lifts it, #396): the practice buys
    nothing until ``krx-quant trade rearm``, which counts again from then, the practice still out."""
    fake, account = kis_mock
    rules = whole_rules(capital=80_000, cumulative_loss_percent=2.0)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_run(at("2026-10-13 09:00:00"), MOCK, {})
    finally:
        store.close()
    kept_trade(tmp_path, "2026-10-13 10:00:00", "2026-10-13 10:04:00", 68_000, practice=True)
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_halt(at("2026-10-13 10:04:00"), MOCK, CUMULATIVE_LIMIT, -2_000, -2_000, 0.0, 1_600)
    finally:
        store.close()
    frozen_here(tmp_path)
    held = MockDesk(tmp_path, fake, account, rules=rules, selection=practised(), practice=True)

    await held.run([TEN, BOUGHT])

    assert held.status == CUMULATIVE_LIMIT
    assert [d["reason"] for d in rows(tmp_path, "decisions") if d["mode"] == MOCK] == [CUMULATIVE_LIMIT]
    assert held.state_now is not None
    cumulative = held.state_now["cumulative"]
    assert (cumulative["realized"], cumulative["practice"]["realized"]) == (0.0, -2_000)
    assert held.state_now["limits"]["cumulative"]["used"] == 0.0
    assert held.state_now["limits"]["cumulative"]["reached_at"] == "2026-10-13T10:04:00+09:00"

    store = TraderStore(tmp_path / DB_NAME)
    try:
        asked = limits.ask(store, REARM, MOCK, at("10:05:00"))
    finally:
        store.close()
    assert not isinstance(asked, str)
    again = MockDesk(
        tmp_path, fake, account, rules=rules, started_at=at("10:06:00"), selection=practised(), practice=True
    )
    await again.run([Read("10:08:02.3", [Barrier("10:08:00")]), quote("10:08:02.5", 70_000, 70_100)])

    assert again.status == RUNNING
    [trade, _] = again.trades()
    assert trade["practice"] and trade["entry"]["at"] == "2026-10-14T10:08:02.500000+09:00"
    assert again.state_now is not None
    cumulative = again.state_now["cumulative"]
    assert cumulative["since"] == "2026-10-14T10:06:00+09:00"
    assert (cumulative["realized"], cumulative["practice"]["realized"]) == (0.0, 0.0), (
        "counted from the re-arm"
    )


# The pairs


def test_the_pairs_leave_the_practice_out_and_hold_every_other_trade_once(tmp_path: Path) -> None:
    """A practice trade and a practice shadow's trade from before #548 pair with nothing and are left
    out of the pairs; the champion policy's shadow trade of the same prediction has no KIS trade of
    its book; two KIS trades of one prediction pair with none, each listed alone. The live gap measures
    every KIS leg, the practice's among them, against its decision quote (#548): none has one here."""
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
        made(1, PAPER, champion=False, shadow=True, practice=True, price=70_100)  # from before #548
        made(2, MOCK, champion=True, shadow=False, practice=False, price=70_050)
        made(2, MOCK, champion=True, shadow=False, practice=False, price=70_050)  # twice: pairs none
        made(2, PAPER, champion=False, shadow=True, practice=False, price=70_100)
        found = pairing(store.db, MOCK, [DAY])
        legs = live.legs(store.db, MOCK, [DAY], 0.0)
        store.db.row_factory = sqlite3.Row
        gap = day_gap(store.db, DAY)
    finally:
        store.close()

    assert found.pairs == ()
    assert [(m is None, s is None) for m, s in found.alone] == [
        (True, False),  # the champion's shadow of prediction 1: no KIS trade of its book
        (False, True),
        (False, True),
        (True, False),
    ]
    assert legs.values == () and legs.total == 6, "three KIS trades, the practice's among them"
    said = live.gap_check("gap", legs, corrected=False).said
    assert said == "KIS 다리 6개 가운데 결정 호가로 잰 다리가 없어 체결 차이를 재지 못했다"
    assert gap is not None and len(gap["pairs"]) == 4, "every trade but the practice's in exactly one"
    assert (gap["mock"]["trades"], gap["shadow"]["trades"]) == (2, 2)
    assert all("practice" not in p for p in gap["pairs"])
