"""The trader under its champion policy (#399): the champion's cells among those the cost map passes,
in its order, at its thresholds and Kelly fractions; trades carrying its version; nothing traded
when it cannot be read; the store from before policies.

Input: policies kept as the nightly re-fit keeps them (``policy.add``), picks as the cost map passes
them, predictions and quotes as the trader reads them (``tests/trader_doubles.py``). Output: the
picks the trader trades, its decisions and its trades.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from krx_quant.config import Settings
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell
from krx_quant.trader.paper import BELOW_THRESHOLD, OUTRANKED
from krx_quant.trader.policy import PolicyCell, PolicyError
from krx_quant.trader.run import run_trader, under_champion
from krx_quant.trader.selection import NO_PASS, NO_POLICY, NOT_IN_POLICY, Selection, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore, read_trades
from tests.test_live_predictions import free_port
from tests.test_trader_run import EngineApi, rows, until
from tests.trader_doubles import (
    BARRIER,
    FEE,
    Barrier,
    Day,
    Read,
    at,
    cost_map,
    cost_row,
    pick,
    predictor,
    quote,
)

FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")


def cell(of: Cell, threshold: float = 0.7, kelly_fraction: float = 0.25, role: str = "top") -> PolicyCell:
    return PolicyCell(of.kind, role, of.predictor, threshold, kelly_fraction, {"trades": 24})


def champion(state: Path, *cells: PolicyCell) -> policies.Policy:
    """A policy kept as the nightly re-fit keeps it: the first, so the champion."""
    db = policies.connect(state / DB_NAME)
    try:
        version, first = policies.add(db, at("22:31:00"), "밤 맞추기", cells, [], {"grid": {}})
        found = policies.champion(db)
    finally:
        db.close()
    assert first and found is not None and found.version == version
    return found


def test_the_champion_s_cells_trade_at_its_threshold_and_kelly_fraction(tmp_path: Path) -> None:
    """θ 0.7: the 10:00 rise at 0.65 buys nothing. λ 0.25 of f* = 2: half the capital, 7 shares at
    70,100. The trade carries the policy's version and its θ."""
    kept = champion(tmp_path, cell(BARRIER, threshold=0.7, kelly_fraction=0.25))
    passing = Selection((replace(pick(BARRIER), kelly=2.0),))

    under = policies.apply(kept, passing)
    trades = Day(tmp_path, selection=under).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", p=(0.65, 0.2, 0.15))]),
            quote("10:00:02.5", 70_000, 70_100),
            Read("10:05:02.3", [Barrier("10:05:00", p=(0.75, 0.15, 0.10))]),
            quote("10:05:02.5", 70_000, 70_100),
        ]
    )

    [p] = under.picks
    assert (p.threshold, p.fraction, under.policy) == (0.7, 0.5, 1)
    [trade] = trades
    assert (trade["policy"], trade["threshold"], trade["fraction"]) == (1, 0.7, 0.5)
    assert trade["entry"]["quantity"] == 7, "⌊500,000 ÷ 70,100⌋"
    decisions = Day(tmp_path).decisions()
    assert [(d["action"], d["reason"]) for d in decisions] == [("skip", BELOW_THRESHOLD), ("buy", None)]
    [selection] = Day(tmp_path).selections()
    assert selection["policy_id"] == 1
    assert json.loads(selection["picks"])[0]["threshold"] == 0.7


def test_a_cell_passing_that_the_champion_lacks_is_not_traded(tmp_path: Path) -> None:
    kept = champion(tmp_path, cell(FIFTEEN, role="baseline"))
    body = cost_map(
        [
            cost_row("barrier", top=predictor("breiman", 0.70, "pass")),
            cost_row("barrier_15m", baseline=predictor(FIFTEEN.predictor, 0.66, "fail")),
        ]
    )

    under = policies.apply(kept, choose(body))

    assert (under.picks, under.reason, under.policy) == ((), NOT_IN_POLICY, 1)
    seen = {(c["kind"], c["predictor"]): (c["picked"], c["why"]) for c in under.considered}
    assert seen[("barrier", "breiman")] == (False, "not in policy 1"), "it passes, but is not the champion's"
    assert seen[("barrier_15m", FIFTEEN.predictor)] == (False, None), "the champion's, but it fails today"
    assert under.describe().startswith("none of policy 1's cells passes: ")


def test_the_champion_s_order_ranks_the_rises_of_a_bar_close(tmp_path: Path) -> None:
    """The cost map ranks the barrier's breiman first (0.70 − 0.62 against 0.66 − 0.60); the champion
    put the 15-minute baseline first. Both rise at 10:00: the baseline buys."""
    kept = champion(tmp_path, cell(FIFTEEN, role="baseline", threshold=0.6), cell(BARRIER, threshold=0.6))
    ranked = Selection((pick(BARRIER, low=0.70, p_star=0.62), pick(FIFTEEN, role="baseline", low=0.66)))

    under = policies.apply(kept, ranked)
    trades = Day(tmp_path, selection=under).run(
        [
            Read(
                "10:00:02.3",
                [Barrier("10:00:00"), Barrier("10:00:00", kind=FIFTEEN.kind, predictor=FIFTEEN.predictor)],
            ),
            quote("10:00:02.5", 70_000, 70_100),
        ]
    )

    assert [p.cell for p in under.picks] == [FIFTEEN, BARRIER]
    [trade] = trades
    assert trade["predictor"] == FIFTEEN.predictor
    reasons = {d["predictor"]: d["reason"] for d in Day(tmp_path).decisions()}
    assert reasons == {FIFTEEN.predictor: None, BARRIER.predictor: OUTRANKED}


def test_before_the_first_champion_the_cost_map_s_cells_trade_as_before(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    passing = Selection((pick(BARRIER),))
    try:
        assert store.champion() is None
        assert under_champion(store, passing) == passing
    finally:
        store.close()
    [trade] = Day(tmp_path, selection=passing).run(
        [Read("10:00:02.3", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)]
    )
    assert (trade["policy"], trade["threshold"]) == (None, 0.6), "the .env's θ, no policy"


def test_a_champion_that_cannot_be_read_trades_nothing(tmp_path: Path) -> None:
    champion(tmp_path, cell(BARRIER))
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.execute("UPDATE policies SET cells = ?", (json.dumps([{"kind": "barrier", "threshold": 2}]),))
        db.commit()
    store = TraderStore(tmp_path / DB_NAME)
    try:
        with pytest.raises(PolicyError, match="policy 1"):
            store.champion()
        under = under_champion(store, Selection((pick(BARRIER),)))
    finally:
        store.close()

    assert (under.picks, under.reason) == ((), NO_POLICY)
    assert "policy 1" in (under.detail or "")


def test_a_stored_cell_is_read_for_its_values_only() -> None:
    """A limit or the capital, were one ever written beside a cell's values, is not read: a policy
    has no limit to hand the trader."""
    stored = {**cell(BARRIER).as_json(), "capital": 10_000_000, "daily_loss_limit": 0.5}

    read = PolicyCell.from_json(stored)

    assert read == cell(BARRIER)
    assert not hasattr(read, "capital")
    with pytest.raises(ValueError):
        PolicyCell.from_json({**stored, "kelly_fraction": 0})
    with pytest.raises(ValueError):
        PolicyCell.from_json({**stored, "threshold": 1.5})


def test_the_store_from_before_policies_gets_their_tables_and_columns(tmp_path: Path) -> None:
    path = tmp_path / DB_NAME
    with closing(sqlite3.connect(path)) as db:
        db.executescript(BEFORE)
        db.commit()

    TraderStore(path).close()

    with closing(sqlite3.connect(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        trade_columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
        pick_columns = {r[1] for r in db.execute("PRAGMA table_info(selections)")}
    assert {"policies", "champions"} <= tables
    assert "policy_id" in trade_columns and "policy_id" in pick_columns
    assert policies.read_policies(path, 10) == {"champion": None, "policies": [], "champions": []}


async def test_the_trader_picks_under_the_champion_at_its_start(tmp_path: Path) -> None:
    """The cost map passes the barrier's breiman and the champion trades it at θ 0.8: the 10:00 rise
    at 0.7 is below it."""
    state = tmp_path / "state"
    engine = PredictionStore(state / ENGINE_DB)
    Barrier("10:00:00").write(engine)
    engine.close()
    champion(state, cell(BARRIER, threshold=0.8))
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=state, fee_rate=FEE)
    api = EngineApi([cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])])
    async with api.serving() as engine_url:
        trader = TraderSettings(
            quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes", engine_url=engine_url, poll_seconds=0.01
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_trader(
                settings,
                trader,
                stop=stop,
                now=lambda: at("10:00:02.3"),
                sleep=lambda seconds: asyncio.sleep(min(seconds, 0.02)),
            )
        )
        try:
            await until(lambda: decided(state) == 1)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    [selection] = rows(state, "selections")
    assert (selection["policy_id"], json.loads(selection["picks"])[0]["threshold"]) == (1, 0.8)
    [decision] = rows(state, "decisions")
    assert (decision["action"], decision["reason"]) == ("skip", BELOW_THRESHOLD)
    assert read_trades(state / DB_NAME, None, 10) == []


def decided(state: Path) -> int:
    """The decisions kept; none while the trader has not made its tables in the store the policy
    was kept in before it started."""
    try:
        return len(rows(state, "decisions"))
    except sqlite3.OperationalError:
        return 0


def test_an_empty_champion_trades_nothing(tmp_path: Path) -> None:
    kept = champion(tmp_path)

    under = policies.apply(kept, Selection((pick(BARRIER),)))
    nothing = policies.apply(kept, Selection((), reason=NO_PASS))

    assert (under.picks, under.reason) == ((), NOT_IN_POLICY)
    assert nothing.reason == NO_PASS, "why none passes stays the cost map's"


# The selections and trades tables as #395 made them (before #399).
BEFORE = """
CREATE TABLE selections (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, mode TEXT NOT NULL, as_of TEXT, picks TEXT NOT NULL,
    considered TEXT NOT NULL, reason TEXT, detail TEXT
);
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
    predictor TEXT NOT NULL, prediction_id INTEGER NOT NULL, bar_close TEXT NOT NULL,
    probability REAL NOT NULL, threshold REAL NOT NULL, anchor REAL NOT NULL, tick REAL NOT NULL,
    width_ticks INTEGER NOT NULL, target REAL NOT NULL, stop REAL NOT NULL, time_barrier TEXT NOT NULL,
    decided_at TEXT NOT NULL, entry_at TEXT NOT NULL, entry_price REAL NOT NULL, quantity INTEGER NOT NULL,
    buy_fee REAL NOT NULL, exit_at TEXT, exit_price REAL, exit_reason TEXT, sell_fee REAL, tax REAL,
    cost REAL, gross REAL, net REAL, selection_id INTEGER, role TEXT, p_star REAL, precision_low REAL,
    kelly REAL, fraction REAL
);
"""
