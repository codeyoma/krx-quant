"""The verdict policy (#677, ``verdicts.py``): on a day no champion cell trades, the mock book trades
the engine's verdict (#676), sized by the loss budget, and leaves at the verdict's target, stop or
deadline, or when a later verdict says SELL while it holds the trade. Through the trader's desk, the
engine's database it reads and the fake KIS (``tests/test_trader_mock.py``).

The example: the 10:00 verdict (made at 10:00:03 from one 15-minute member saying up first 0.7,
timeout 0.2, down first 0.1, width 5 ticks about 70,050) says BUY: target 70,550, stop 69,550, deadline
10:15. The first quote after it asks 70,100: the loss budget (0.5% of 1,000,000 won) over a share's
loss at the stop (550 + one tick + the costs, about 810 won) buys 6 shares; KIS fills them at 70,050.
Every value is made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from krx_quant.config import SettingsError
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL, VERDICT, report
from krx_quant.trader.book import (
    BUY_LIMIT,
    PAST_LAST_EXIT,
    SPACING,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME,
    VERDICT_SELL,
    VERDICT_WAIT,
)
from krx_quant.trader.cells import EngineReader
from krx_quant.trader.decisions import read_decisions
from krx_quant.trader.selection import choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DAILY_LIMIT, DB_NAME
from krx_quant.trader.verdicts import CELLS
from krx_quant.verdict import BUY, SELL, VERDICT_VERSION, WAIT, Held
from tests.kis_doubles import Fill, kis_mock, rows
from tests.test_trader_practice import FAILING, PASSING, crowned, frozen_here
from tests.test_trader_shadow import DAY, Event, MockDesk
from tests.trader_doubles import BARRIER, CALENDAR, Barrier, Read, Said, at, policy_cell, quote, whole_rules

__all__ = ["kis_mock"]  # the fixture, used here

# The trader's loss budget (0.5%), the other rules the tests'.
RULES = whole_rules(risk_percent=0.5)
BOOK = quote("10:00:03.5", 70_000, 70_100)  # the first quote after the 10:00 verdict
BOUGHT_AT = (70_050.0, 6)
# The trade the 10:00 BUY opens, as the store keeps it: its time barrier the verdict's deadline.
HELD = Held(70_050.0, 70_550.0, 69_550.0, at("10:15:00"))


def said(hm: str, *, read_at: float = 3.3, **verdict: Any) -> Read:
    """The verdict of the bar close ``HH:MM`` (``Said``), read ``read_at`` seconds after it."""
    return Read(f"{hm}:{read_at:04.1f}", [Said(f"{hm}:00", **verdict)])


def decisions(state: Path) -> dict[str, tuple[str, str | None]]:
    """The mock book's verdict decisions by bar close (``HH:MM``): buy, or skip and why."""
    return {
        d["bar_close"][11:16]: (d["action"], d["reason"])
        for d in rows(state, "decisions")
        if d["mode"] == MOCK and d["predictor"] == VERDICT
    }


def verdict_desk(state: Path, fake: FakeKis, account: FakeAccount, **changes: Any) -> MockDesk:
    """The mock desk with the verdict on, on a cost map no cell passes and no champion yet."""
    given: dict[str, Any] = {"rules": RULES, "selection": choose(FAILING), "verdict": True}
    return MockDesk(state, fake, account, **(given | changes))


# What a BUY buys


async def test_a_buy_verdict_buys_on_kis_by_the_loss_budget_with_the_verdict_s_barriers(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK])

    [trade] = desk.trades()
    assert (trade["mode"], trade["kind"], trade["predictor"], trade["role"]) == (
        MOCK,
        "barrier_15m",
        VERDICT,
        VERDICT,
    )
    assert (trade["entry"]["price"], trade["entry"]["quantity"]) == BOUGHT_AT
    assert (trade["target"], trade["stop"], trade["time_barrier"]) == (
        70_550,
        69_550,
        "2026-10-14T10:15:00+09:00",
    )
    # Of no policy and no practice: the trader's own trade, which the loss limits count.
    assert (trade["champion"], trade["policy"], trade["practice"], trade["exit"]) == (True, None, False, None)
    # The verdict that opened it, its version first, so that its trades are scored per version.
    opened = trade["verdict"]
    assert (opened["version"], opened["verdict"], opened["horizon"]) == (
        VERDICT_VERSION,
        BUY,
        {"kind": "barrier_15m", "minutes": 15},
    )
    assert opened["bar_close"] == "2026-10-14T10:00:00+09:00" and opened["expected_net"] > 0
    [(version,)] = _column(tmp_path, "SELECT verdict_version FROM trades")
    assert version == VERDICT_VERSION
    assert decisions(tmp_path) == {"10:00": ("buy", None)}


def _column(state: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return db.execute(sql).fetchall()


async def test_a_wait_or_an_avoiding_sell_buys_nothing_and_sends_nothing_to_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run(
        [
            said("10:00", p=(0.3, 0.4, 0.3)),  # no edge: WAIT
            quote("10:00:03.5", 70_000, 70_100),
            said("10:01", p=(0.1, 0.2, 0.7)),  # down first: SELL, avoid (nothing held)
            quote("10:01:03.5", 70_000, 70_100),
        ]
    )

    assert decisions(tmp_path) == {"10:00": ("skip", VERDICT_WAIT), "10:01": ("skip", VERDICT_SELL)}
    assert desk.trades() == [] and rows(tmp_path, "unfilled_buys") == [] and account.orders == []


# How it leaves


async def test_a_verdict_trade_takes_profit_at_the_verdict_s_target(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK, quote("10:05:00", 70_600, 70_700), Fill("10:05:30")])

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == TAKE_PROFIT and trade["net"] > 0
    # The nightly report counts it apart, by its verdict's version: not as the champion's.
    lines = report.render(report.read(tmp_path / DB_NAME, DAY, CALENDAR)).splitlines()
    assert any(line.startswith(f"판정 v{VERDICT_VERSION}(KIS 모의) | 1건 |") for line in lines)
    assert "챔피언 없음 | 0건 | - | - | - | - | - | -" in lines


async def test_a_verdict_trade_is_stopped_at_the_verdict_s_stop(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK, quote("10:05:00", 69_500, 69_600)])

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == STOP_LOSS and trade["exit"]["at"] == "2026-10-14T10:05:00+09:00"


async def test_a_verdict_trade_is_sold_at_the_verdict_s_deadline(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run(
        [said("10:00"), BOOK, quote("10:14:59", 70_000, 70_100), quote("10:15:00.5", 70_000, 70_100)]
    )

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == TIME


async def test_a_later_sell_verdict_sells_the_verdict_trade_at_the_next_quote(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The 10:05 verdict, made with the trade held, turns down: SELL (exit). The trade sells at the bid
    of the first quote after it, ``verdict_sell``, before its barriers."""
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run(
        [
            said("10:00"),
            BOOK,
            said("10:05", p=(0.1, 0.2, 0.7), held=HELD),
            quote("10:05:03.5", 70_000, 70_100),
        ]
    )

    [trade] = desk.trades()
    assert trade["exit"]["reason"] == VERDICT_SELL and trade["exit"]["at"].startswith("2026-10-14T10:05:03")
    assert [o["purpose"] for o in trade["orders"]][-1] == VERDICT_SELL
    assert decisions(tmp_path) == {"10:00": ("buy", None), "10:05": ("skip", VERDICT_SELL)}


async def test_a_holding_verdict_keeps_the_trade(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run([said("10:00"), BOOK, said("10:05", held=HELD), quote("10:05:03.5", 70_000, 70_100)])

    [trade] = desk.trades()
    assert trade["exit"] is None
    assert decisions(tmp_path)["10:05"] == ("skip", VERDICT_WAIT)


# The book's rules hold it


async def test_the_loss_limit_counts_a_verdict_trade_and_then_blocks_the_verdict(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A day's loss limit of 0.3% (3,000 won): the 10:00 trade stopped out loses more, so the day stops
    and the 10:20 BUY buys nothing (``daily_limit``)."""
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account, rules=whole_rules(risk_percent=0.5, daily_loss_percent=0.3))

    await desk.run(
        [
            said("10:00"),
            BOOK,
            quote("10:05:00", 69_500, 69_600),
            said("10:20"),
            quote("10:20:03.5", 70_000, 70_100),
        ]
    )

    [trade] = desk.trades()
    assert trade["net"] < -3_000
    assert desk.status == DAILY_LIMIT
    assert decisions(tmp_path)["10:20"] == ("skip", DAILY_LIMIT)


# The 10:00 trade takes profit at 10:05, then BUY verdicts at 10:10 and 10:15.
SPACED: list[Event] = [
    said("10:00"),
    BOOK,
    quote("10:05:00", 70_600, 70_700),
    Fill("10:05:30"),
    said("10:10"),
    quote("10:10:03.5", 70_000, 70_100),
    said("10:15"),
    quote("10:15:03.5", 70_000, 70_100),
]


async def test_verdict_buys_are_spaced_as_the_practice_s(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The 10:10 BUY, ten minutes after the last verdict buy's bar close, is spaced (15 minutes, as the
    practice's, #598); the 10:15 one buys."""
    fake, account = kis_mock
    await verdict_desk(tmp_path, fake, account).run(SPACED)

    assert decisions(tmp_path) == {"10:00": ("buy", None), "10:10": ("skip", SPACING), "10:15": ("buy", None)}


async def test_verdict_buys_count_in_the_day_s_buy_orders(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """With a day of one buy order, the 10:00 buy takes it: the later BUYs are held (``buy_limit``)."""
    fake, account = kis_mock
    await verdict_desk(tmp_path, fake, account, rules=whole_rules(risk_percent=0.5, daily_buys=1)).run(SPACED)

    assert decisions(tmp_path) == {
        "10:00": ("buy", None),
        "10:10": ("skip", BUY_LIMIT),
        "10:15": ("skip", BUY_LIMIT),
    }


async def test_a_buy_whose_deadline_is_past_15_15_buys_nothing(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_mock
    desk = verdict_desk(tmp_path, fake, account)

    await desk.run([said("15:05", minutes=15), quote("15:05:03.5", 70_000, 70_100)])

    assert decisions(tmp_path) == {"15:05": ("skip", PAST_LAST_EXIT)}


# Beside the champion and the practice


async def test_the_verdict_is_not_picked_while_a_champion_cell_trades(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """A champion cell trades: the champion's book reads its cell alone and buys on it as ever; the
    10:00 BUY verdict is not read."""
    fake, account = kis_mock
    crowned(tmp_path, at("08:00:00"), policy_cell(BARRIER))
    desk = verdict_desk(tmp_path, fake, account, selection=choose(PASSING))

    await desk.run([Read("10:00:03.3", [Barrier("10:00:00"), Said("10:00:00")]), BOOK])

    [trade] = desk.trades()
    assert (trade["kind"], trade["predictor"], trade["policy"], trade["verdict"]) == (
        "barrier",
        "breiman",
        1,
        None,
    )
    assert decisions(tmp_path) == {}


async def test_the_verdict_ranks_before_the_practice_cell_and_one_position_waits_at_a_time(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The practice cell's 10:00 rise is read first; it waits for the 10:00 verdict, ranked above it,
    whose BUY buys; the practice rise is outranked. The practice's 10:05 rise finds the verdict trade
    held."""
    fake, account = kis_mock
    frozen_here(tmp_path)
    desk = verdict_desk(tmp_path, fake, account, practice=True)

    await desk.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00")]),
            said("10:00"),
            BOOK,
            Read("10:05:02.3", [Barrier("10:05:00")]),
        ]
    )

    [trade] = desk.trades()
    assert (trade["predictor"], trade["practice"]) == (VERDICT, False)
    practice = {
        d["bar_close"][11:16]: d["reason"] for d in rows(tmp_path, "decisions") if d["predictor"] == "breiman"
    }
    assert practice == {"10:00": "outranked", "10:05": "holding"}
    found = read_decisions(tmp_path / DB_NAME, None, at("10:00:00").date())
    assert [b["book"] for b in found] == ["practice", VERDICT]


# Reading the verdicts


def test_the_reader_reads_each_bar_close_s_newest_version_once(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / ENGINE_DB)
    Said("10:00:00").write(store)
    Said("10:00:00", p=(0.3, 0.4, 0.3), version=VERDICT_VERSION + 1).write(store)
    Said("10:01:00", p=(0.1, 0.2, 0.7)).write(store)
    store.close()
    reader = EngineReader(tmp_path / ENGINE_DB, SYMBOL)
    reader.follow(CELLS, at("09:59:00"))

    read = reader.read(at("00:00:00"))
    again = reader.read(at("00:00:00"))

    assert read is not None and again is not None
    found = [(s.bar_close, s.call.version, s.call.action) for s in read[1] if s.call is not None]
    assert found == [(at("10:00:00"), VERDICT_VERSION + 1, WAIT), (at("10:01:00"), VERDICT_VERSION, SELL)]
    assert again[1] == []
    assert reader.cells == list(CELLS)


def test_an_engine_database_from_before_the_verdicts_gives_none(tmp_path: Path) -> None:
    PredictionStore(tmp_path / ENGINE_DB).close()
    with closing(sqlite3.connect(tmp_path / ENGINE_DB)) as db:
        db.execute("DROP TABLE verdicts")
    reader = EngineReader(tmp_path / ENGINE_DB, SYMBOL)
    reader.follow(CELLS, at("09:59:00"))

    assert reader.read(at("00:00:00")) == ([], [])
    reader.follow([], at("09:59:00"))
    assert reader.cells == []


# The setting


def test_the_verdict_is_on_in_mock_mode_unless_set_off_and_refused_in_the_others() -> None:
    assert TraderSettings.from_env({"KRX_TRADER_MODE": MOCK}, None).mock_verdict
    assert not TraderSettings.from_env(
        {"KRX_TRADER_MODE": MOCK, "KRX_TRADER_MOCK_VERDICT": "off"}, None
    ).mock_verdict
    assert not TraderSettings.from_env({}, None).mock_verdict, "paper: off unless set"
    for mode in (PAPER, "live"):
        with pytest.raises(SettingsError, match="KRX_TRADER_MOCK_VERDICT=on"):
            TraderSettings.from_env({"KRX_TRADER_MODE": mode, "KRX_TRADER_MOCK_VERDICT": "on"}, None)
    with pytest.raises(SettingsError, match="KRX_TRADER_MOCK_VERDICT must be on or off"):
        TraderSettings.from_env({"KRX_TRADER_MODE": MOCK, "KRX_TRADER_MOCK_VERDICT": "maybe"}, None)
