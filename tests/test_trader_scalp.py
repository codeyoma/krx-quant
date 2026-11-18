"""The scalp policy (#705, ``scalp.py``): on the picks no champion cell trades, the mock book buys one
share when the 4-minute barrier pool's up first is at the threshold or more, its target the level that
nets +0.2% on the fill, its stop 0.3% below it, raised to the level that nets +0.1% once a bid reaches it,
sold at market by the deadline. Through the trader's desk, the engine's database it reads and the fake
KIS (``tests/test_trader_mock.py``), each exit by a replayed quote stream.

The example: the 10:00 verdict's 4-minute pool (one member, up first 0.35, timeout 0.4, down first 0.25,
width 5 ticks about 70,050). The first quote after it asks 70,100; KIS fills one share at 70,050. On that
fill (commission 0.015% a side, the sell tax 0.20%) the target is 70,400 (70,050 × 1.00215 ÷ 0.99785 =
70,351.9, rounded up to the tick of 100), the level that nets +0.1% 70,300 (70,281.7 rounded up), the
stop 69,839.85 (0.3% below). Every value is made up.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.config import SettingsError
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.trader import MOCK, SCALP, report
from krx_quant.trader.book import (
    BELOW_THRESHOLD,
    BUY_LIMIT,
    PAST_LAST_EXIT,
    RAISED_STOP,
    SCALP_LOSS_CAP,
    STALE,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME,
    Plan,
    ScalpRules,
    plan_scalp,
    scalp_held,
    scalp_raise,
)
from krx_quant.trader.decisions import read_decisions
from krx_quant.trader.money import net_of
from krx_quant.trader.paper import PaperTrader
from krx_quant.trader.selection import choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.api_schema import check
from tests.kis_doubles import Fill, kis_mock, rows
from tests.test_trader_practice import FAILING
from tests.test_trader_shadow import DAY, Event, MockDesk
from tests.trader_doubles import CALENDAR, FEE, Read, Said, at, quote, whole_rules

__all__ = ["kis_mock"]  # the fixture, used here

RULES = whole_rules()
BOOK = quote("10:00:03.5", 70_000, 70_100)  # the first quote after the 10:00 verdict
FILLED = 70_050.0
TARGET, RAISE_AT, STOP = 70_400.0, 70_300.0, 70_050 * 0.997


def said(hm: str, p: tuple[float, float, float] = (0.35, 0.4, 0.25), **verdict: Any) -> Read:
    """The verdict of the bar close ``HH:MM`` with a 4-minute pool saying ``p``, read 3.3 s after it."""
    return Read(f"{hm}:03.3", [Said(f"{hm}:00", p=p, minutes=4, **verdict)])


def decisions(state: Path) -> dict[str, tuple[str, str | None]]:
    """The mock book's scalp decisions by bar close (``HH:MM``): buy, or skip and why."""
    return {
        d["bar_close"][11:16]: (d["action"], d["reason"])
        for d in rows(state, "decisions")
        if d["mode"] == MOCK and d["predictor"] == SCALP
    }


def scalp_desk(state: Path, fake: FakeKis, account: FakeAccount, **changes: Any) -> MockDesk:
    """The mock desk with the scalp on, on a cost map no cell passes and no champion yet."""
    given: dict[str, Any] = {"rules": RULES, "selection": choose(FAILING), "scalp": True}
    return MockDesk(state, fake, account, **(given | changes))


# The levels


def test_the_levels_net_what_they_say_on_the_fill_price() -> None:
    rules = ScalpRules()
    assert rules.target(FILLED, FEE) == TARGET and rules.raise_at(FILLED, FEE) == RAISE_AT
    assert rules.stop(FILLED) == pytest.approx(STOP)
    # Sold at the target, a share nets +0.2% of the fill at least, a tick below it less.
    assert net_of(FILLED, TARGET, 1, FEE) >= 0.002 * FILLED > net_of(FILLED, TARGET - 100, 1, FEE)
    assert net_of(FILLED, RAISE_AT, 1, FEE) >= 0.001 * FILLED > net_of(FILLED, RAISE_AT - 100, 1, FEE)
    # The stop is 0.3% gross, about 0.53% net.
    assert net_of(FILLED, STOP, 1, FEE) / FILLED == pytest.approx(-0.0053, abs=0.0001)


def test_the_stop_is_raised_once_a_bid_reaches_the_level_and_never_lowered() -> None:
    assert scalp_raise(FILLED, STOP, RAISE_AT - 100, RULES) is None
    assert scalp_raise(FILLED, STOP, RAISE_AT, RULES) == RAISE_AT
    assert scalp_raise(FILLED, RAISE_AT, RAISE_AT + 100, RULES) is None  # raised once


def test_the_plan_buys_at_the_threshold_by_15_15() -> None:
    from krx_quant.trader.scalp import pool_signal

    session = CALENDAR.session(DAY)
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        body = Said("10:00:00", p=(0.30, 0.45, 0.25), minutes=4).made()
        row = db.execute(
            "SELECT 1 AS id, ? AS bar_close, 2 AS version, ? AS made_at, ? AS body",
            (at("10:00:00").isoformat(), at("10:00:03").isoformat(), json.dumps(body)),
        ).fetchone()
        signal = pool_signal(row, "005930")
    assert signal.rise_probability == 0.30 and signal.scalp is not None
    planned = plan_scalp(signal, RULES, session, at("10:00:03.3"))
    assert isinstance(planned, Plan)
    assert (planned.target, planned.time_barrier) == (70_400.0, at("10:15:00"))
    assert planned.stop == pytest.approx(70_050 * 0.997)
    assert plan_scalp(signal, RULES, session, at("10:00:20")) == STALE
    higher = whole_rules(scalp=ScalpRules(threshold=0.31))
    assert plan_scalp(signal, higher, session, at("10:00:03.3")) == BELOW_THRESHOLD


def test_its_own_risk_rules_hold_its_buys() -> None:
    rules = whole_rules(daily_buys=1, scalp=ScalpRules(daily_buys=2, daily_loss=500))
    now = at("11:00:00")
    assert scalp_held(rules, now, 1, []) is None  # the book's one buy a day is not its cap
    assert scalp_held(rules, now, 2, []) == BUY_LIMIT
    assert scalp_held(rules, now, 0, [(at("10:30:00"), -300.0)]) is None
    assert scalp_held(rules, now, 0, [(at("10:30:00"), -300.0), (at("10:45:00"), -200.0)]) == SCALP_LOSS_CAP


# What it buys, and how it leaves


async def test_an_up_first_at_the_threshold_buys_a_share_and_places_the_target_at_once(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK])

    [trade] = desk.trades()
    assert (trade["kind"], trade["predictor"], trade["role"], trade["scalp"]) == (
        "barrier",
        SCALP,
        SCALP,
        True,
    )
    assert (trade["entry"]["price"], trade["entry"]["quantity"]) == (FILLED, 1)
    assert (trade["target"], trade["stop"], trade["time_barrier"]) == (
        TARGET,
        pytest.approx(STOP),
        "2026-10-14T10:15:00+09:00",
    )
    assert (trade["champion"], trade["policy"], trade["practice"], trade["exit"]) == (True, None, False, None)
    [resting] = [o for o in trade["orders"] if o["purpose"] == TAKE_PROFIT]
    assert (resting["price"], resting["quantity"], resting["status"]) == (TARGET, 1, "open")
    signal = trade["scalp_signal"]
    assert (signal["source"], signal["p_up"], signal["threshold"], signal["raise_at"]) == (
        "verdict_pool_4m",
        0.35,
        0.30,
        RAISE_AT,
    )
    assert decisions(tmp_path) == {"10:00": ("buy", None)}
    assert desk.announced == [
        "[buy] 스캘프 매수(mock): 삼성전자(005930) 1주 70,050원 · 목표가 70,400원 · 손절가 69,840원"
        " · 마감 10:15"
    ]


async def test_an_up_first_below_the_threshold_buys_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run([said("10:00", p=(0.29, 0.46, 0.25)), BOOK])

    assert decisions(tmp_path) == {"10:00": ("skip", BELOW_THRESHOLD)}
    assert desk.trades() == [] and account.orders == []


async def test_the_target_fills_on_kis(kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK, quote("10:03:00", 70_400, 70_500), Fill("10:03:30")])

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == TAKE_PROFIT and trade["exit"]["price"] == TARGET
    assert trade["net"] >= 0.002 * FILLED
    assert (
        desk.announced[-1]
        == f"[sell] 스캘프 매도(mock): 삼성전자(005930) 1주 · 목표가 도달 · 순손익 {trade['net']:+,.0f}원"
    )


async def test_a_bid_at_the_raise_level_raises_the_stop_and_a_fall_to_it_sells_with_a_profit(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The 10:02 bid reaches 70,300: the stop is raised there, kept on the trade. The 10:04 bid of 70,300
    sells it (``raised_stop``), above its fill price after the costs."""
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run(
        [said("10:00"), BOOK, quote("10:02:00", 70_300, 70_400), quote("10:04:00", 70_300, 70_400)]
    )

    [trade] = desk.trades()
    assert trade["stop"] == RAISE_AT
    assert trade["exit"]["reason"] == RAISED_STOP and trade["exit"]["at"].startswith("2026-10-14T10:04")
    assert trade["net"] >= 0.001 * FILLED
    assert " · 올린 손절가 도달 · " in desk.announced[-1]


async def test_a_fall_through_the_stop_sells_at_a_loss(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK, quote("10:02:00", 69_800, 69_900)])

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == STOP_LOSS and trade["net"] < 0


async def test_neither_by_the_deadline_sells_at_market(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run(
        [said("10:00"), BOOK, quote("10:14:59", 70_100, 70_200), quote("10:15:00.5", 70_100, 70_200)]
    )

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == TIME
    assert " · 마감 시한 · " in desk.announced[-1]


async def test_a_deadline_past_15_15_buys_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = scalp_desk(tmp_path, fake, account)

    await desk.run([said("15:01"), quote("15:01:03.5", 70_000, 70_100)])

    assert decisions(tmp_path) == {"15:01": ("skip", PAST_LAST_EXIT)}


# Its own book


STOPPED_THEN_AGAIN: list[Event] = [
    said("10:00"),
    BOOK,
    quote("10:02:00", 69_800, 69_900),  # stopped
    said("10:20"),
    quote("10:20:03.5", 70_000, 70_100),
]


async def test_its_buy_cap_is_its_own(kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path) -> None:
    """With a scalp cap of one buy a day, the 10:20 signal is held (``buy_limit``); the book's own cap of
    one buy a day is not used by it."""
    fake, account = kis_mock
    rules = whole_rules(daily_buys=1, scalp=ScalpRules(daily_buys=1))
    desk = scalp_desk(tmp_path, fake, account, rules=rules)

    await desk.run(STOPPED_THEN_AGAIN)

    assert decisions(tmp_path) == {"10:00": ("buy", None), "10:20": ("skip", BUY_LIMIT)}
    assert desk.state_now is not None and desk.state_now["risk"]["buys"] == 0, "the book's day counts none"


async def test_its_daily_loss_cap_is_its_own_and_the_champion_s_limits_count_none_of_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A champion's daily limit of 0.01% (100 won) and a scalp cap of 100 won: the 10:00 stop loses more
    than either. The scalp's own cap holds its 10:20 buy (``scalp_loss_cap``); the champion's limit is
    not reached (the trader runs on)."""
    fake, account = kis_mock
    rules = whole_rules(daily_loss_percent=0.01, scalp=ScalpRules(daily_loss=100))
    desk = scalp_desk(tmp_path, fake, account, rules=rules)

    await desk.run(STOPPED_THEN_AGAIN)

    [trade] = desk.trades()
    assert trade["net"] < -100
    assert desk.status == "running"
    assert decisions(tmp_path) == {"10:00": ("buy", None), "10:20": ("skip", SCALP_LOSS_CAP)}
    assert desk.state_now is not None and desk.state_now["today"]["net"] == 0, "the limits count none of it"


async def test_the_decisions_api_has_a_scalp_book(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    await scalp_desk(tmp_path, fake, account).run([said("10:00"), BOOK])

    found = read_decisions(tmp_path / DB_NAME, None, at("10:00:00").date())
    assert [b["book"] for b in found] == [SCALP]


async def test_the_nightly_report_shows_the_scalp_with_its_net_winning_rate(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A target at 10:03, a stop at 10:22: one of two trades netted a profit."""
    fake, account = kis_mock
    await scalp_desk(tmp_path, fake, account).run(
        [
            said("10:00"),
            BOOK,
            quote("10:03:00", 70_400, 70_500),
            Fill("10:03:30"),
            said("10:20"),
            quote("10:20:03.5", 70_000, 70_100),
            quote("10:22:00", 69_800, 69_900),
        ]
    )

    lines = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR)).splitlines()

    [day] = [line for line in lines if line.startswith("그날: 매매 2건")]
    assert "순이익 매매 50%(1/2)" in day and "청산: 목표 1, 올린 손절 0, 손절 1, 마감 0" in day
    assert "최대 낙폭 " in day and "매매당 순손익 " in day
    assert any(line.startswith("누적(2026-10-14부터): 매매 2건") for line in lines)
    assert any("이 목표(순 +0.2% 먼저, -0.3% 전, 15분)로 학습하지 않았다" in line for line in lines)
    assert any(line.startswith("스캘프(KIS 모의) | 2건 |") for line in lines)
    assert "챔피언 없음 | 0건 | - | - | - | - | - | -" in lines
    assert "매매 없음." in lines, "the scalp's trades are not the champion's"
    assert any(line.startswith("스캘프(KIS 모의, KRX_TRADER_MOCK_SCALP, #705): ") for line in lines)
    assert any(line.startswith("매매할 칸 없음: ") for line in lines), "the scalp's pick is no champion cell"


# The settings


def test_on_by_default_in_mock_mode_alone() -> None:
    assert TraderSettings.from_env({"KRX_TRADER_MODE": "mock"}, None).mock_scalp
    assert not TraderSettings.from_env({"KRX_TRADER_MODE": "paper"}, None).mock_scalp
    assert not TraderSettings.from_env(
        {"KRX_TRADER_MODE": "mock", "KRX_TRADER_MOCK_SCALP": "off"}, None
    ).mock_scalp
    for mode in ("paper", "live"):
        with pytest.raises(SettingsError, match="KRX_TRADER_MOCK_SCALP=on is for KRX_TRADER_MODE=mock alone"):
            TraderSettings.from_env({"KRX_TRADER_MODE": mode, "KRX_TRADER_MOCK_SCALP": "on"}, None)


def test_its_rules_are_settings() -> None:
    found = TraderSettings.from_env(
        {
            "KRX_TRADER_MODE": "mock",
            "KRX_TRADER_MOCK_SCALP_THRESHOLD": "0.4",
            "KRX_TRADER_MOCK_SCALP_TARGET_PERCENT": "0.15",
            "KRX_TRADER_MOCK_SCALP_RAISE_PERCENT": "0.05",
            "KRX_TRADER_MOCK_SCALP_STOP_PERCENT": "0.25",
            "KRX_TRADER_MOCK_SCALP_DEADLINE_MINUTES": "10",
            "KRX_TRADER_MOCK_SCALP_DAILY_BUY_ORDERS": "5",
            "KRX_TRADER_MOCK_SCALP_DAILY_LOSS_WON": "2000",
        },
        None,
    )
    from krx_quant.trader.book import Rules

    rules = Rules.of(found, FEE).scalp
    assert rules == ScalpRules(0.4, 0.15, 0.05, 0.25, timedelta(minutes=10), 5, 2000.0)
    with pytest.raises(SettingsError, match="must be below KRX_TRADER_MOCK_SCALP_TARGET_PERCENT"):
        TraderSettings.from_env({"KRX_TRADER_MOCK_SCALP_RAISE_PERCENT": "0.2"}, None)
    with pytest.raises(SettingsError, match="scalp buy orders a day"):
        TraderSettings.from_env({"KRX_TRADER_MOCK_SCALP_DAILY_BUY_ORDERS": "21"}, None)


async def test_a_provisional_stop_above_the_fill_is_set_again_and_only_a_raised_one_kept(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A buy whose fills are read later (``mock.KisFills._absorb``) carries barriers priced at the ask it
    was sent at: should the fill come below that stop, the stop is set again on the fill, not kept as a
    raised one. A stop raised on a quote is kept."""
    fake, account = kis_mock
    await scalp_desk(tmp_path, fake, account).run([said("10:00"), BOOK])
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        db.execute("UPDATE trades SET stop = 71000")  # provisional, above the fill of 70,050
    store = TraderStore(tmp_path / DB_NAME)
    try:
        book = PaperTrader(store, RULES, CALENDAR.session, at("10:01:00"), MOCK)
        trade = store.open_trade(MOCK)
        assert trade is not None and not trade.stop_raised
        again = book.scalp_filled(trade)
        assert again.target == TARGET and again.stop == pytest.approx(STOP)
        store.set_barriers(trade.id, TARGET, RAISE_AT, raised=True)
        raised_trade = store.open_trade(MOCK)
        assert raised_trade is not None and raised_trade.stop_raised
        assert book.scalp_filled(raised_trade).stop == RAISE_AT
    finally:
        store.close()


async def test_an_exit_after_the_day_is_open_in_that_day_s_report(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    await scalp_desk(tmp_path, fake, account).run([said("10:00"), BOOK, quote("10:02:00", 69_800, 69_900)])
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        db.execute("UPDATE trades SET exit_at = '2026-10-15T09:10:00+09:00'")

    lines = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR)).splitlines()

    assert "그날: 매매 1건(열림 1) · 그날 매수 주문 1/?" in lines
    assert any(line.startswith("누적(2026-10-14부터): 매매 1건(열림 1)") for line in lines)


# The switch to the scalp kind's ranking member (#706)
#
# The engine's ranking of the scalp kind of 2026-10-13 has one member, breiman-scalp, ranked first; a
# second algorithm ranked below it is no member. Its 10:00 prediction says P(target first) 0.70 about
# the mid-point 70,050. There the break-even is 0.6634: the target 70,400 nets +0.2686% (70,400 ×
# 0.99785 − 70,050 × 1.00015 = 188.13 won), the stop 69,839.85 −0.5294% (370.79 won), and
# 370.79 ÷ (188.13 + 370.79) = 0.6634. Every value is made up.

MEMBER_NAME = "breiman-scalp"
BREAK_EVEN = 370.79 / (188.13 + 370.79)


def ranking_body(members: dict[str, int | None]) -> dict[str, Any]:
    """A scalp ranking as the engine keeps it, its rows the algorithms with their ranks; those with a
    rank are members, as ``ranking.Snapshot.as_json`` gives them (only the fields read here)."""
    rows = [
        {"rank": rank, "algorithm": name, "member": rank is not None, "ranked": rank is not None}
        for name, rank in members.items()
    ]
    return {"kind": "scalp", "night": "2026-10-13", "version": 1, "rows": rows}


def engine_with(state: Path, members: dict[str, int | None], p_target: float = 0.70) -> None:
    """The engine's database before the day: the scalp ranking of the night before (``members``), and
    each algorithm's 10:00 scalp prediction displayed 2 s after it, saying ``p_target`` first."""
    from krx_quant.predictions import DB_NAME as ENGINE_DB
    from krx_quant.predictions import NewKindPrediction, PredictionStore

    store = PredictionStore(state / ENGINE_DB)
    try:
        night = DAY - timedelta(days=1)
        store.add_rankings([(night, "scalp", 1, at("15:40:00"), None, ranking_body(members))])
        close = at("10:00:00")
        output = {"p_target_first": p_target, "p_timeout": 0.2, "p_stop_first": round(0.8 - p_target, 6)}
        store.add_kind_predictions(
            "scalp",
            [
                NewKindPrediction(
                    symbol="005930",
                    algorithm=name,
                    model_version="v1",
                    bar_close=close,
                    displayed_at=close + timedelta(seconds=2),
                    output=output,
                    anchor=70_050.0,
                    tick=100,
                )
                for name in members
            ],
        )
    finally:
        store.close()


def test_the_break_even_is_the_stop_s_net_loss_over_both_payoffs() -> None:
    rules = ScalpRules()
    p = rules.break_even(FILLED, FEE)
    assert p == pytest.approx(BREAK_EVEN, abs=1e-4)
    won, lost = net_of(FILLED, TARGET, 1, FEE), net_of(FILLED, STOP, 1, FEE)
    assert p * won + (1 - p) * lost == pytest.approx(0.0, abs=1e-9), "nets nothing on average"
    # Without the tick's rounding the target nets +0.2% alone: 0.529 / (0.2 + 0.529).
    assert ScalpRules().break_even(70_000, FEE) < p < 0.0052935 / (0.002 + 0.0052935)


def test_the_policy_reads_the_rankings_of_the_ranking_s_version() -> None:
    from krx_quant.ranking import RANKING_VERSION
    from krx_quant.trader import scalp

    assert scalp.RANKING_VERSION == RANKING_VERSION


def test_the_member_ranked_highest_is_read(tmp_path: Path) -> None:
    from krx_quant.predictions import DB_NAME as ENGINE_DB
    from krx_quant.trader import scalp

    assert scalp.source(tmp_path / ENGINE_DB, RULES) == scalp.Source(scalp.POOL), "no engine database"
    engine_with(tmp_path, {"zeta-scalp": 2, MEMBER_NAME: 1, "breiman-scalp-b": None})
    found = scalp.source(tmp_path / ENGINE_DB, RULES)
    assert found == scalp.Source(scalp.MEMBER, MEMBER_NAME) and found.cell == scalp.MEMBER_CELL


def test_without_a_member_the_pool_is_read(tmp_path: Path) -> None:
    from krx_quant.predictions import DB_NAME as ENGINE_DB
    from krx_quant.trader import scalp

    engine_with(tmp_path, {MEMBER_NAME: None})
    assert scalp.source(tmp_path / ENGINE_DB, RULES) == scalp.Source(scalp.POOL)


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"scalp": ScalpRules(target_percent=0.25)}, "target 0.25% net, the kind's 0.2%"),
        ({"scalp": ScalpRules(stop_percent=0.4)}, "stop 0.4% gross, the kind's 0.3%"),
        ({"scalp": ScalpRules(deadline=timedelta(minutes=10))}, "deadline 10 minutes, the kind's 15"),
        ({"fee_rate": 0.0002}, "commission 0.0002 a side, the kind's 0.00015"),
    ],
)
def test_levels_other_than_the_kind_s_keep_the_pool_and_say_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, changes: dict[str, Any], said: str
) -> None:
    from krx_quant.predictions import DB_NAME as ENGINE_DB
    from krx_quant.trader import scalp

    engine_with(tmp_path, {MEMBER_NAME: 1})
    rules = whole_rules(**changes)
    assert scalp.differs(RULES) is None
    with caplog.at_level("WARNING", logger="krx_quant.trader.scalp"):
        found = scalp.source(tmp_path / ENGINE_DB, rules)
    assert found == scalp.Source(scalp.POOL, MEMBER_NAME, said)
    assert any(MEMBER_NAME in r.getMessage() and said in r.getMessage() for r in caplog.records)


async def test_with_a_member_the_scalp_buys_on_its_target_first_at_the_break_even(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The member's 0.70 is above the break-even 0.6634: a share bought. The verdict's 4-minute pool of
    the same bar close is not read."""
    fake, account = kis_mock
    engine_with(tmp_path, {MEMBER_NAME: 1, "zeta-scalp": None})
    desk = scalp_desk(tmp_path, fake, account, engine=True)

    await desk.run([said("10:00", p=(0.9, 0.05, 0.05)), BOOK])

    [trade] = desk.trades()
    assert (trade["kind"], trade["predictor"], trade["scalp"]) == ("scalp", SCALP, True)
    assert (trade["entry"]["price"], trade["target"], trade["stop"]) == (FILLED, TARGET, pytest.approx(STOP))
    signal = trade["scalp_signal"]
    assert (signal["source"], signal["member"], signal["p_up"]) == ("scalp_member", MEMBER_NAME, 0.70)
    assert signal["threshold"] == pytest.approx(BREAK_EVEN, abs=1e-4)
    found = read_decisions(tmp_path / DB_NAME, None, DAY)
    [scalped] = [d for b in found if b["book"] == SCALP for d in b["decisions"]]
    assert (scalped["kind"], scalped["scalp_signal"], scalped["action"]) == ("scalp", "scalp_member", "buy")
    assert scalped["threshold"] is None, "the break-even differs by price: kept on the trade"
    check("trader_trade", trade)
    check("trader_decisions", {"day": DAY.isoformat(), "book": None, "books": found})
    assert scalped["answer"] is None, "not answered yet"

    # Once the engine answers the bar close, the decision has its answer, read by the member's prediction
    # though its predictor is the scalp's.
    from krx_quant.kinds import Answer
    from krx_quant.predictions import DB_NAME as ENGINE_DB
    from krx_quant.predictions import PredictionStore

    engine = PredictionStore(tmp_path / ENGINE_DB)
    try:
        engine.add_kind_answers("scalp", 1, [("005930", at("10:00:00"), Answer("target_first"), False)])
    finally:
        engine.close()
    answered = read_decisions(tmp_path / DB_NAME, tmp_path / ENGINE_DB, DAY, SCALP)
    [decided] = answered[0]["decisions"]
    assert (decided["answer"], decided["right"]) == ("up", True)

    lines = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR)).splitlines()
    [read] = [line for line in lines if line.startswith("신호: ")]
    assert f"스캘프 종류의 순위 멤버(목표 먼저 확률, #706) {MEMBER_NAME} 매수 1건" in read
    assert "그날 문턱 0.663–0.663" in read and "학습하지 않았다" not in read
    picked = "스캘프(KIS 모의, KRX_TRADER_MOCK_SCALP, #705): 스캘프 종류의 순위 멤버"
    assert any(line.startswith(picked) for line in lines)


async def test_a_member_s_target_first_below_the_break_even_buys_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    engine_with(tmp_path, {MEMBER_NAME: 1}, p_target=0.66)
    desk = scalp_desk(tmp_path, fake, account, engine=True)

    await desk.run([Read("10:00:03.3"), BOOK])

    assert decisions(tmp_path) == {"10:00": ("skip", BELOW_THRESHOLD)}
    assert desk.trades() == [] and account.orders == []


async def test_with_levels_other_than_the_kind_s_the_pool_s_signal_stays(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    engine_with(tmp_path, {MEMBER_NAME: 1})
    rules = whole_rules(scalp=ScalpRules(stop_percent=0.4))
    desk = scalp_desk(tmp_path, fake, account, engine=True, rules=rules)

    await desk.run([said("10:00"), BOOK])

    [trade] = desk.trades()
    assert (trade["kind"], trade["scalp_signal"]["source"], trade["scalp_signal"]["threshold"]) == (
        "barrier",
        "verdict_pool_4m",
        0.30,
    )
    found = read_decisions(tmp_path / DB_NAME, None, DAY)
    [scalped] = [d for b in found if b["book"] == SCALP for d in b["decisions"]]
    assert scalped["scalp_signal"] == "verdict_pool_4m"


def test_the_engine_reader_reads_the_scalp_cell_picked(tmp_path: Path) -> None:
    from krx_quant.trader import scalp
    from krx_quant.trader.cells import EngineReader

    reader = EngineReader(tmp_path / "engine.sqlite3", "005930")
    reader.follow([scalp.CELL], at("09:00:00"))
    assert reader.cells == [scalp.CELL]
    reader.follow([scalp.MEMBER_CELL], at("09:00:00"))
    assert reader.cells == [scalp.MEMBER_CELL]
    reader.follow([], at("09:00:00"))
    assert reader.cells == []
