"""What to do now (#672, spec #671, ``trader/action.py``): the card follows the champion's book alone,
``buy`` for a minute after its buy, ``sell`` while it sells and for a minute after, ``wait`` with why
otherwise, the first that holds; and ``GET /v1/action`` reads it from the trader's store.

Input: the trader's state, trades and champion policy as ``GET /v1/trader``, ``/v1/trader/trades`` and
the store give them, built by hand; and one paper day of the whole desk written into a store. Every
price, probability and figure is made up.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import Session
from krx_quant.trader import SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.action import BUY, SELL, WAIT, card, read_action
from tests.api_schema import check
from tests.test_trader_decisions import desk_day

DAY = date(2026, 10, 14)
SESSION = Session(DAY, datetime(2026, 10, 14, 9, 0, tzinfo=KST), datetime(2026, 10, 14, 15, 30, tzinfo=KST))


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-14T{clock}+09:00")


def trader(**fields: Any) -> dict[str, Any]:
    """A running trader's state with nothing held, as ``GET /v1/trader`` gives it."""
    return {"mode": "paper", "alive": True, "status": "running", "position": None, "risk": None} | fields


def trade(
    entered: str, *, exited: str | None = None, reason: str | None = None, **fields: Any
) -> dict[str, Any]:
    """A champion's trade of the barrier's breiman (``GET /v1/trader/trades``), open unless ``exited``."""
    return {
        "id": 7,
        "symbol": SYMBOL,
        "kind": "barrier",
        "predictor": "breiman",
        "practice": False,
        "shadow": False,
        "tick": 100.0,
        "target": 70_700.0,
        "stop": 69_300.0,
        "time_barrier": "2026-10-14T10:14:00+09:00",
        "entry": {"at": f"2026-10-14T{entered}+09:00", "price": 70_100.0, "quantity": 7},
        "exit": None
        if exited is None
        else {"at": f"2026-10-14T{exited}+09:00", "price": 70_700.0, "reason": reason},
    } | fields


def champion(*, cells: bool = True) -> policies.Policy:
    """Policy 1 trading the barrier's breiman at θ 0.6, its night's record: 980 signals, 943 bought,
    512 netting above 0, +0.31 ticks a signal (low +0.15) over 20 days; or with no cell."""
    fitted = {
        "signals": {
            "0.6": {
                "signals": 980,
                "bought": 943,
                "wins": 512,
                "per_signal": {"value": 0.31, "low": 0.15, "high": 0.47, "days": 20},
            }
        }
    }
    kept = (policies.PolicyCell("barrier", "top", "breiman", 0.6, fitted),) if cells else ()
    return policies.Policy(1, at("22:31:00") - timedelta(days=1), "test", kept)


def the_card(
    state: dict[str, Any],
    trades: list[dict[str, Any]],
    now: str,
    *,
    policy: policies.Policy | None = None,
    unreadable: bool = False,
    session: Session | None = SESSION,
) -> dict[str, Any]:
    return card(
        SYMBOL,
        "삼성전자",
        trader=state,
        trades=trades,
        champion=policy,
        unreadable=unreadable,
        now=at(now),
        session=session,
        tick=100.0,
    )


def test_a_buy_is_shown_for_a_minute_from_its_fill_with_its_plan_and_record() -> None:
    position = {"trade_id": 7, "bid": 70_000.0, "bid_at": "2026-10-14T10:10:30+09:00", "selling": False}

    got = the_card(trader(position=position), [trade("10:10:02.5")], "10:10:30", policy=champion())

    assert (got["action"], got["why"]) == (BUY, "bought")
    assert (
        got["said"] == "자동 매매가 10:10:02에 70,100원에 샀습니다. 아래 가격에 닿거나 10:14이 되면 팝니다."
    )
    assert (got["since"], got["until"]) == (
        "2026-10-14T10:10:02.500000+09:00",
        "2026-10-14T10:11:02.500000+09:00",
    )
    assert got["plan"] == {
        "entry_price": 70_100.0,
        "entry_at": "2026-10-14T10:10:02.5+09:00",
        "target": 70_700.0,
        "stop": 69_300.0,
        "sell_by": "2026-10-14T10:14:00+09:00",
        "bid": 70_000.0,
        "bid_at": "2026-10-14T10:10:30+09:00",
    }
    assert got["record"] == {
        "kind": "barrier",
        "predictor": "breiman",
        "threshold": 0.6,
        "days": 20,
        "signals": 980,
        "bought": 943,
        "wins": 512,
        "per_signal_ticks": 0.31,
        "low_ticks": 0.15,
        "tick": 100.0,
        "per_signal_won": 31.0,
        "low_won": 15.0,
    }


def test_after_its_minute_the_open_trade_is_held_and_waits_with_its_plan() -> None:
    position = {"trade_id": 7, "bid": 70_200.0, "bid_at": "2026-10-14T10:12:00+09:00", "selling": False}

    got = the_card(trader(position=position), [trade("10:10:02.5")], "10:11:02.5", policy=champion())

    assert (got["action"], got["why"]) == (WAIT, "holding")
    assert got["said"] == "자동 매매가 들고 있습니다. 아래 가격에 닿거나 10:14이 되면 팝니다."
    assert got["plan"] is not None and got["plan"]["bid"] == 70_200.0
    assert got["until"] is None


def test_a_sale_begun_says_sell_and_why_as_the_state_tells() -> None:
    selling = {"trade_id": 7, "bid": 69_200.0, "selling": True}
    cases = [
        (trader(position=selling), "10:12:00", "stop_loss", "손절 가격에 닿았습니다"),
        (trader(position=selling | {"bid": 70_000.0}), "10:14:00", "time", "팔 시각이 되었습니다"),
        (trader(position=selling, status="paused"), "10:12:00", "pause", "즉시 매도 정지를 요청했습니다"),
        (
            trader(position=selling, status="daily_limit"),
            "10:12:00",
            "daily_limit",
            "하루 손실 한도에 닿았습니다",
        ),
        (trader(position=selling | {"bid": 70_000.0}), "10:12:00", "selling", None),
    ]
    for state, now, why, said in cases:
        got = the_card(state, [trade("10:10:02.5")], now)

        assert (got["action"], got["why"]) == (SELL, why), why
        assert got["said"] == (
            "자동 매매가 팔고 있습니다." if said is None else f"자동 매매가 팔고 있습니다: {said}."
        )


def test_a_sale_is_shown_for_a_minute_then_the_card_waits_again() -> None:
    sold = trade("10:10:02.5", exited="10:12:40", reason="take_profit")

    got = the_card(trader(), [sold], "10:13:00", policy=champion())

    assert (got["action"], got["why"]) == (SELL, "take_profit")
    assert got["said"] == "자동 매매가 10:12:40에 70,700원에 팔았습니다: 이익 실현 가격에 닿았습니다."
    assert got["exit"] == {"reason": "take_profit", "price": 70_700.0, "at": "2026-10-14T10:12:40+09:00"}
    assert got["until"] == "2026-10-14T10:13:40+09:00"

    later = the_card(trader(), [sold], "10:13:40", policy=champion())

    assert (later["action"], later["why"], later["exit"]) == (WAIT, "no_signal", None)


def test_wait_says_the_first_reason_that_holds() -> None:
    held = {"held": "cooldown", "cooldown_until": "2026-10-14T10:42:30+09:00"}
    cases = [
        (trader(alive=False), "10:00:00", champion(), False, "trader_off"),
        (trader(), "09:04:59", champion(), False, "outside_hours"),
        (trader(), "15:15:00", champion(), False, "outside_hours"),
        (trader(status="daily_limit"), "10:00:00", champion(), False, "stopped"),
        (trader(), "10:00:00", None, True, "no_policy"),
        (trader(), "10:00:00", None, False, "no_cell"),
        (trader(), "10:00:00", champion(cells=False), False, "no_cell"),
        (trader(risk=held), "10:00:00", champion(), False, "held"),
        (trader(risk={"held": "buy_limit"}), "10:00:00", champion(), False, "held"),
        (trader(), "09:05:00", champion(), False, "no_signal"),
    ]
    for state, now, policy, unreadable, why in cases:
        got = the_card(state, [], now, policy=policy, unreadable=unreadable)

        assert (got["action"], got["why"]) == (WAIT, why), (now, why)
        assert got["plan"] is None
    assert (
        the_card(trader(risk=held), [], "10:00:00", policy=champion())["said"]
        == "손실이 연달아 10:42까지 쉽니다."
    )
    assert the_card(trader(), [], "10:00:00", session=None)["why"] == "outside_hours"  # not a trading day


def test_the_record_is_the_first_cell_s_while_waiting_and_none_without_a_cell() -> None:
    assert the_card(trader(), [], "10:00:00", policy=champion())["record"]["signals"] == 980
    assert the_card(trader(), [], "10:00:00", policy=champion(cells=False))["record"] is None


def test_practice_and_shadow_trades_are_never_the_card_s() -> None:
    position = {"trade_id": 7, "bid": 70_000.0, "selling": False}
    for flag in ("practice", "shadow"):
        flagged = trade("10:10:02.5") | {flag: True}
        got = the_card(trader(position=position), [flagged], "10:10:30")

        assert (got["action"], got["why"], got["plan"]) == (WAIT, "no_cell", None), flag


def test_a_trader_stopped_while_holding_waits_and_keeps_the_plan() -> None:
    got = the_card(trader(alive=False), [trade("10:10:02.5")], "10:10:30", policy=champion())

    assert (got["action"], got["why"]) == (WAIT, "trader_off")
    assert got["said"] == "자동 매매가 멈춰 있어 판단하지 않습니다. 들고 있는 것은 아래 계획대로 팝니다."
    assert got["plan"] is not None


def test_without_a_store_the_card_waits_for_the_trader(tmp_path: Path) -> None:
    for path in (None, tmp_path / "trader.sqlite3"):
        body = read_action(path, at("10:00:00"), lambda _: SESSION, {SYMBOL: "삼성전자"}, {SYMBOL: 100.0})

        check("action", body)
        assert (body["alive"], body["mode"]) == (False, None)
        assert [(c["symbol"], c["name"], c["action"], c["why"]) for c in body["symbols"]] == [
            (SYMBOL, "삼성전자", WAIT, "trader_off")
        ]


def test_the_store_s_champion_trade_makes_the_card_at_its_moment(tmp_path: Path) -> None:
    desk_day(tmp_path)  # the champion buys the 10:10 rise at 10:10:02.5 and sells it at its time

    cards = {
        now: read_action(tmp_path / "trader.sqlite3", at(now), lambda _: SESSION, {}, {})
        for now in ("10:12:00", "10:15:30", "10:16:10")
    }

    for body in cards.values():
        check("action", body)
    held, sold, after = (body["symbols"][0] for body in cards.values())
    # The test's trader wrote no state now, so the card says so, keeping the trade's plan.
    assert (held["action"], held["why"]) == (WAIT, "trader_off")
    assert held["plan"] is not None and held["plan"]["entry_at"].startswith("2026-10-14T10:10:02")
    assert (sold["action"], sold["why"]) == (SELL, "time")
    assert sold["exit"] is not None and sold["exit"]["at"].startswith("2026-10-14T10:15:02")
    assert sold["record"] is None  # the doubles' policy cell keeps no signals of the night
    assert (after["action"], after["why"], after["plan"]) == (WAIT, "trader_off", None)
