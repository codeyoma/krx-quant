"""The challengers in paper beside the champion (#400, ``desk.py``): the same predictions and the same
quotes, trades and decisions split by policy version, no limit nor pause on a challenger, the
records of the days each ran, a restart, and the store from before #400.

The example of ``test_trader_paper.py``: the 10:00 barrier prediction (up_first 0.7) read at
10:00:02.3, the first quote after it asking 70,100 (14 shares), the target 70,250, the stop 69,850,
the time barrier 10:04. Policy 1, the champion, was kept on the night of 10-12; policy 2, a
challenger, on the night of 10-13: both run on 10-14. Every price is made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from krx_quant.trader import policy as policies
from krx_quant.trader import promotion
from krx_quant.trader.limits import ENTRIES_PAUSED, RUNNING
from krx_quant.trader.paper import BELOW_THRESHOLD, STOP_LOSS, TAKE_PROFIT, TIME, Rules
from krx_quant.trader.store import DB_NAME, TraderStore, read_trader
from tests.trader_doubles import (
    BARRIER,
    FEE,
    AskPause,
    Barrier,
    Day,
    Read,
    Repick,
    Tick,
    at,
    keep,
    policy_cell,
    quote,
)

TEN = Read("10:00:02.3", [Barrier("10:00:00")])
BOUGHT = quote("10:00:02.5", 70_000, 70_100)
TAKEN = quote("10:02:00", 70_300, 70_400)


def champion_and_challenger(state: Path, champion_threshold: float, challenger_threshold: float) -> None:
    assert keep(state, at("2026-10-12 22:31:00"), policy_cell(BARRIER, champion_threshold)) == 1
    assert keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, challenger_threshold)) == 2


def decisions(day: Day) -> list[tuple[int | None, int, str, str | None]]:
    return [(d["policy_id"], d["prediction_id"], d["action"], d["reason"]) for d in day.decisions()]


def test_a_challenger_trades_in_paper_beside_the_champion_on_the_same_prediction_and_quotes(
    tmp_path: Path,
) -> None:
    """The champion's θ 0.8 skips the 0.7 rise; the challenger's θ 0.6 buys it on the first quote
    after it and sells at the target. Its trade carries its version and is not the champion's."""
    champion_and_challenger(tmp_path, 0.8, 0.6)
    day = Day(tmp_path, desk=True)

    champions = day.run([TEN, BOUGHT, TAKEN])

    assert champions == [], "the champion bought nothing"
    [trade] = day.trades(policy=2)
    assert (trade["policy"], trade["champion"], trade["threshold"]) == (2, False, 0.6)
    assert trade["entry"] == {"at": "2026-10-14T10:00:02.500000+09:00", "price": 70_100, "quantity": 14}
    assert trade["exit"]["reason"] == TAKE_PROFIT
    [prediction] = {d[1] for d in decisions(day)}
    assert decisions(day) == [(1, prediction, "skip", BELOW_THRESHOLD), (2, prediction, "buy", None)], (
        "one prediction, a decision in each book"
    )
    assert day.trades(policy=1) == []


def test_the_champion_and_a_challenger_alike_make_the_same_trade_apart(tmp_path: Path) -> None:
    champion_and_challenger(tmp_path, 0.6, 0.6)
    day = Day(tmp_path, desk=True)

    [champion] = day.run([TEN, BOUGHT, TAKEN])

    [challenger] = day.trades(policy=2)
    assert (champion["policy"], champion["champion"], challenger["champion"]) == (1, True, False)
    same = ("entry", "exit", "net", "target", "stop", "time_barrier")
    assert {k: champion[k] for k in same} == {k: challenger[k] for k in same}
    assert champion["id"] != challenger["id"]
    assert champion["net"] == pytest.approx(-161.735)


def test_a_challenger_s_loss_is_not_in_the_limits_and_no_limit_stops_it(tmp_path: Path) -> None:
    """The daily limit narrowed to 0.5% (5,000 won). The challenger's stop at 69,800 loses 6,448.19,
    past it: the champion is still running, its day's P&L 0, and it buys the 10:10 rise at 0.85.
    The challenger buys that rise too: no limit holds it either."""
    champion_and_challenger(tmp_path, 0.8, 0.6)
    day = Day(tmp_path, desk=True, rules=Rules(fee_rate=FEE, daily_loss_percent=0.5))

    [champion] = day.run(
        [
            TEN,
            BOUGHT,
            quote("10:01:30", 69_800, 69_900),  # the challenger's stop
            Read("10:10:02.3", [Barrier("10:10:00", p=(0.85, 0.1, 0.05))]),
            quote("10:10:02.5", 70_000, 70_100),
            quote("10:12:00", 70_300, 70_400),
        ]
    )

    stopped, later = sorted(day.trades(policy=2), key=lambda t: t["id"])
    assert (stopped["exit"]["reason"], stopped["net"]) == (STOP_LOSS, pytest.approx(-6_448.19))
    assert later["exit"]["reason"] == TAKE_PROFIT
    assert (champion["bar_close"][11:16], champion["exit"]["reason"]) == ("10:10", TAKE_PROFIT)
    state = day.state_now("10:12:00")
    assert state["status"] == RUNNING
    assert state["today"]["realized"] == pytest.approx(champion["net"]), "the champion's trade alone"
    assert state["cash"] == pytest.approx(1_000_000 + champion["net"])
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert db.execute("SELECT count(*) FROM halts").fetchone()[0] == 0


def test_a_pause_holds_the_champion_alone_and_marks_its_day_stopped(tmp_path: Path) -> None:
    champion_and_challenger(tmp_path, 0.6, 0.6)
    day = Day(tmp_path, desk=True)

    champions = day.run([AskPause("09:30:00"), TEN, BOUGHT, TAKEN])

    assert champions == []
    [challenger] = day.trades(policy=2)
    assert challenger["exit"]["reason"] == TAKE_PROFIT, "no pause holds a challenger"
    assert day.state_now("10:02:00")["status"] == ENTRIES_PAUSED
    assert records(tmp_path) == {
        1: [("2026-10-14", 0.0, 0, True, True)],
        2: [("2026-10-14", pytest.approx(challenger["net"]), 1, False, False)],
    }, "the champion's day is stopped: no common day for a promotion"


def records(state: Path) -> dict[int, list[tuple[str, float, int, bool, bool]]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        kept = promotion.records(db)
    return {
        v: [(d.day.isoformat(), d.net, d.trades, d.champion, d.stopped) for d in r.days]
        for v, r in kept.items()
    }


def test_each_policy_running_keeps_the_day_it_ran_while_the_session_is_open(tmp_path: Path) -> None:
    """Started at 08:00: nothing is kept before the open. A day without a trade is kept all the
    same: 0 in the record."""
    champion_and_challenger(tmp_path, 0.8, 0.9)
    day = Day(tmp_path, desk=True, started_at=at("08:00:00"))

    day.run([Tick("08:59:59")])
    assert records(tmp_path) == {}
    day.run([Tick("09:00:01"), TEN, BOUGHT, TAKEN])

    assert records(tmp_path) == {
        1: [("2026-10-14", 0.0, 0, True, False)],
        2: [("2026-10-14", 0.0, 0, False, False)],
    }


def test_a_challenger_kept_tonight_runs_from_the_next_trading_day(tmp_path: Path) -> None:
    """Policy 2 kept on the night of 10-14, after its pick: it runs on 10-15 from that day's pick."""
    keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(BARRIER, 0.8))
    keep(tmp_path, at("22:31:00"), policy_cell(BARRIER, 0.6))
    day = Day(tmp_path, desk=True)

    day.run(
        [
            TEN,
            BOUGHT,
            Repick("2026-10-15 08:30:00"),
            Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
            quote("2026-10-15 10:00:02.5", 70_000, 70_100),
            quote("2026-10-15 10:02:00", 70_300, 70_400),
        ]
    )

    [trade] = day.trades(policy=2)
    assert trade["bar_close"][:10] == "2026-10-15", "nothing on 10-14: it was kept that night"
    assert [d[0] for d in records(tmp_path)[2]] == ["2026-10-15"]


def test_a_dropped_challenger_runs_no_more_from_the_next_pick(tmp_path: Path) -> None:
    """Policies 2, 3 and 4 run on 10-14; policy 5, kept that night, drops policy 2, the oldest: on
    10-15 policies 3, 4 and 5 run."""
    keep(tmp_path, at("2026-10-12 22:31:00"), policy_cell(BARRIER, 0.8))
    for made in ("2026-10-13 22:31:00", "2026-10-13 22:32:00", "2026-10-13 22:33:00"):
        keep(tmp_path, at(made), policy_cell(BARRIER, 0.6))
    day = Day(tmp_path, desk=True)
    day.run([Tick("09:00:01")])
    keep(tmp_path, at("22:31:00"), policy_cell(BARRIER, 0.6))

    day.run([Repick("2026-10-15 08:30:00"), Tick("2026-10-15 09:00:01")])

    ran = {v: [d[0] for d in days] for v, days in records(tmp_path).items()}
    assert ran == {
        1: ["2026-10-14", "2026-10-15"],
        2: ["2026-10-14"],
        3: ["2026-10-14", "2026-10-15"],
        4: ["2026-10-14", "2026-10-15"],
        5: ["2026-10-15"],
    }


def test_a_restart_goes_on_with_a_challenger_s_open_trade(tmp_path: Path) -> None:
    """The trader stops at 10:01 with the challenger's trade open; started again at 10:30, past its
    time barrier, the challenger's book sells it on the first quote."""
    champion_and_challenger(tmp_path, 0.8, 0.6)
    Day(tmp_path, desk=True).run([TEN, BOUGHT, Tick("10:01:00")])
    [held] = Day(tmp_path).trades(policy=2)
    assert held["exit"] is None

    Day(tmp_path, desk=True, started_at=at("10:30:00")).run([quote("10:30:05", 70_000, 70_100)])

    [sold] = Day(tmp_path).trades(policy=2)
    assert (sold["exit"]["reason"], sold["exit"]["at"]) == (TIME, "2026-10-14T10:30:05+09:00")
    assert Day(tmp_path).trades() == [], "the champion's book took nothing of it"


def test_the_trader_s_state_shows_the_policies_running_and_the_gate(tmp_path: Path) -> None:
    champion_and_challenger(tmp_path, 0.8, 0.6)
    Day(tmp_path, desk=True).run([TEN, BOUGHT, TAKEN])

    state = read_trader(tmp_path / DB_NAME, at("10:02:00"))

    assert state["champion"] == 1
    assert state["gate"] == {
        "policy": 1,
        "days": 1,
        "required": 20,
        "mean": 0.0,
        "low": None,
        "level": 0.95,
        "passed": False,
    }
    champion, challenger = state["policies"]
    assert (champion["version"], champion["status"], champion["promotion"]) == (1, "champion", None)
    assert challenger["paper"] == {
        "since": "2026-10-14",
        "last": "2026-10-14",
        "days": 1,
        "trades": 1,
        "net": pytest.approx(-161.735),
        "mean": pytest.approx(-161.735),
        "low": None,
    }
    assert challenger["promotion"] == {
        "champion": 1,
        "days": 1,
        "required": 10,
        "mean": pytest.approx(-161.735),
        "low": None,
        "level": 0.95,
        "challengers": 1,
        "passed": False,
        "why": "few_days",
    }


# The decisions table as #395 made it, unique without the policy (before #400).
BEFORE = """
CREATE TABLE selections (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, mode TEXT NOT NULL, as_of TEXT, picks TEXT NOT NULL,
    considered TEXT NOT NULL, reason TEXT, detail TEXT, policy_id INTEGER
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
    predictor TEXT NOT NULL, prediction_id INTEGER NOT NULL, bar_close TEXT NOT NULL,
    displayed_at TEXT NOT NULL, decided_at TEXT NOT NULL, probability REAL, action TEXT NOT NULL,
    reason TEXT, trade_id INTEGER, selection_id INTEGER,
    UNIQUE (mode, kind, predictor, prediction_id)
);
INSERT INTO selections VALUES (1, '2026-10-14T08:30:00+09:00', 'paper', NULL, '[]', '[]', NULL, NULL, NULL);
INSERT INTO selections VALUES (2, '2026-10-15T08:30:00+09:00', 'paper', NULL, '[]', '[]', NULL, NULL, 1);
INSERT INTO decisions VALUES (1, 'paper', '005930', 'barrier', 'breiman', 7, '2026-10-14T10:00:00+09:00',
    '2026-10-14T10:00:02+09:00', '2026-10-14T10:00:02.3+09:00', 0.7, 'skip', 'not_up', NULL, 1);
INSERT INTO decisions VALUES (2, 'paper', '005930', 'barrier', 'breiman', 8, '2026-10-15T10:00:00+09:00',
    '2026-10-15T10:00:02+09:00', '2026-10-15T10:00:02.3+09:00', 0.7, 'skip', 'not_up', NULL, 2);
"""


def test_a_store_from_before_keys_its_decisions_by_the_policy_too(tmp_path: Path) -> None:
    """Its rows take their pick's policy; a prediction read by two books is two decisions; one book's
    second decision on it is refused."""
    path = tmp_path / DB_NAME
    with closing(sqlite3.connect(path)) as db:
        db.executescript(BEFORE)
        db.commit()

    store = TraderStore(path)
    try:
        TraderStore(path).close()  # another writer opens it after: made again once
        assert [
            (r["id"], r["policy_id"]) for r in store._db.execute("SELECT * FROM decisions ORDER BY id")
        ] == [
            (1, None),
            (2, 1),
        ]
        assert store.decided("paper", 1, "barrier", "breiman", 8)
        assert not store.decided("paper", 2, "barrier", "breiman", 8)
        args = ("paper", "005930", "barrier", "breiman", 8, at("10:00:00"), at("10:00:02"), at("10:00:02.3"))
        store.add_decision(*args, 0.7, "skip", "not_up", None, 2)
        with pytest.raises(sqlite3.IntegrityError):
            store.add_decision(*args, 0.7, "skip", "not_up", None, 2)
        with pytest.raises(sqlite3.IntegrityError):
            store.add_decision(*args[:4], 7, *args[5:], 0.7, "skip", "not_up", None, None)
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = [(r[1], r[3]) for r in db.execute("PRAGMA index_list(decisions)")]
    assert "decisions_before_400" not in tables
    assert indexes == [("decisions_by_prediction", "c")]


def test_the_champion_s_records_and_a_challenger_s_are_one_record_each_across_their_roles(
    tmp_path: Path,
) -> None:
    """A policy's record holds its days as a challenger and as the champion (#393: a challenger
    promoted brings its own)."""
    store = TraderStore(tmp_path / DB_NAME)
    try:
        for day, champion in ((date(2026, 10, 14), False), (date(2026, 10, 15), True)):
            store.put_policy_day("paper", 2, day, champion, False, at("10:00:00"))
        store.put_policy_day("paper", 2, date(2026, 10, 15), False, True, at("10:00:00"))  # never undone
    finally:
        store.close()

    assert records(tmp_path) == {
        2: [("2026-10-14", 0.0, 0, False, False), ("2026-10-15", 0.0, 0, True, True)],
    }
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.challenger_versions(db) == []
