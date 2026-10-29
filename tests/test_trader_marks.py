"""``GET /v1/symbols/{symbol}/trader-marks`` (#398): the trader's buys and sells as the chart marks
them, a page at a time, within the /v1 contract (``schemas/engine-api-v1.json``).

The day of ``test_trader_api.py``: a take-profit bought at 10:00:02.5 and sold at 10:02, a stop
bought at 10:05:02.5 and sold at 10:06, and a trade bought at 10:10:02.5 still open. Every price is
made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.trader import SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.store import DB_NAME, Entry, Exit, TraderStore
from tests.api_schema import check
from tests.replay_events import at as replay_at
from tests.replay_events import run
from tests.test_trader_api import DAY
from tests.trader_doubles import Day, at

OTHER = "000660"  # predicted by the engine, never traded


async def marks(scratch: Path, state: Path | None, query: str = "", symbol: str = SYMBOL) -> tuple[int, Any]:
    """The answer of an engine (replayed into ``scratch``, predicting 005930 and 000660) whose state
    folder is ``state``."""
    engine = await run(scratch / "replay.db", [], until=replay_at("09:00:00"), symbols=(SYMBOL, OTHER))
    async with TestClient(TestServer(create_app(engine, state_dir=state))) as client:
        response = await client.get(f"/v1/symbols/{symbol}/trader-marks{query}")
        return response.status, await response.json()


def before(moment: str) -> str:
    return "before=" + url_quote(moment)


def trade(
    store: TraderStore,
    mode: str,
    entry: datetime,
    out: datetime | None,
    prediction_id: int,
    policy_id: int | None = None,
) -> None:
    """A trade of ``mode`` written as the trader writes one: bought at ``entry`` at 70,100, 10
    shares, and sold at ``out`` (if given) at the target; under ``policy_id`` (#399) if given."""
    decision = store.add_decision(
        mode, SYMBOL, "barrier", "breiman", prediction_id, entry, entry, entry, 0.7, "buy", None
    )
    opened = store.enter(
        decision,
        Entry(
            mode=mode,
            symbol=SYMBOL,
            kind="barrier",
            predictor="breiman",
            prediction_id=prediction_id,
            bar_close=entry,
            probability=0.7,
            threshold=0.6,
            anchor=70_050,
            tick=100,
            width_ticks=2,
            target=70_250,
            stop=69_850,
            time_barrier=entry + timedelta(minutes=4),
            decided_at=entry,
            entry_at=entry,
            entry_price=70_100,
            quantity=10,
            buy_fee=105.15,
            quote_at=entry,
            policy_id=policy_id,
        ),
    )
    if out is not None:
        store.exit(opened, Exit(out, 70_250, "take_profit", 105.375, 1405, out))


async def test_each_trade_s_buy_and_its_sell_once_out_newest_first(tmp_path: Path) -> None:
    trades = Day(tmp_path).run(DAY)

    status, body = await marks(tmp_path, tmp_path)

    assert status == 200
    check("trader_marks", body)
    assert body["symbol"] == SYMBOL
    assert [(m["side"], m["at"][11:21]) for m in body["marks"]] == [
        ("buy", "10:10:02.5"),
        ("sell", "10:06:00+0"),
        ("buy", "10:05:02.5"),
        ("sell", "10:02:00+0"),
        ("buy", "10:00:02.5"),
    ]
    open_, stopped, taken = trades
    bought, sold = body["marks"][4], body["marks"][3]
    assert bought == {
        "trade_id": taken["id"],
        "side": "buy",
        "at": "2026-10-14T10:00:02.500000+09:00",
        "price": 70_100,
        "quantity": 14,
        "mode": "paper",
        "policy": None,
        "kind": "barrier",
        "predictor": "breiman",
        "exit_reason": None,
        "net": None,
    }
    assert sold["trade_id"] == taken["id"]
    assert (sold["price"], sold["quantity"]) == (taken["exit"]["price"], 14)
    assert (sold["exit_reason"], sold["net"]) == ("take_profit", taken["net"])
    assert body["marks"][1]["exit_reason"] == "stop_loss" and body["marks"][1]["net"] == stopped["net"]
    assert body["marks"][0]["trade_id"] == open_["id"], "the open trade's buy alone"


async def test_before_and_limit_page_back_through_every_mark_once(tmp_path: Path) -> None:
    Day(tmp_path).run(DAY)
    _, whole = await marks(tmp_path, tmp_path)

    read: list[dict[str, Any]] = []
    query = "?limit=2"
    while True:
        _, page = await marks(tmp_path, tmp_path, query)
        if not page["marks"]:
            break
        read += page["marks"]
        query = f"?limit=2&{before(page['marks'][-1]['at'])}"

    assert read == whole["marks"]
    _, earlier = await marks(tmp_path, tmp_path, f"?{before('2026-10-14T10:05:02.5+09:00')}")
    assert [m["at"][11:19] for m in earlier["marks"]] == ["10:02:00", "10:00:02"], "strictly before"


async def test_each_mark_carries_its_trade_s_mode(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    try:
        trade(store, "paper", at("09:40:01"), at("09:41:00"), 1)
        trade(store, "mock", at("10:00:01"), at("10:01:00"), 2)
        trade(store, "live", at("10:20:01"), None, 3)
    finally:
        store.close()

    _, body = await marks(tmp_path, tmp_path)

    check("trader_marks", body)
    assert [(m["mode"], m["side"]) for m in body["marks"]] == [
        ("live", "buy"),
        ("mock", "sell"),
        ("mock", "buy"),
        ("paper", "sell"),
        ("paper", "buy"),
    ]


async def test_a_page_never_splits_the_marks_of_one_moment(tmp_path: Path) -> None:
    # Two trades filled on the same quote (two modes, or later two policies): a page ending on that
    # moment holds both, or the next page, before it, would miss the one left out.
    store = TraderStore(tmp_path / DB_NAME)
    try:
        trade(store, "paper", at("10:00:01"), None, 1)
        trade(store, "live", at("10:00:01"), None, 2)
        trade(store, "paper", at("09:50:01"), None, 3)
    finally:
        store.close()

    _, page = await marks(tmp_path, tmp_path, "?limit=1")
    _, next_page = await marks(tmp_path, tmp_path, f"?limit=1&{before(page['marks'][-1]['at'])}")

    check("trader_marks", page)
    assert [(m["mode"], m["at"][11:19]) for m in page["marks"]] == [
        ("live", "10:00:01"),
        ("paper", "10:00:01"),
    ]
    assert [m["at"][11:19] for m in next_page["marks"]] == ["09:50:01"]


async def test_no_marks_for_a_symbol_not_traded_nor_without_the_store(tmp_path: Path) -> None:
    Day(tmp_path).run(DAY)

    assert await marks(tmp_path, tmp_path, symbol=OTHER) == (200, {"symbol": OTHER, "marks": []})
    assert await marks(tmp_path, tmp_path / "empty") == (200, {"symbol": SYMBOL, "marks": []})
    assert await marks(tmp_path, None) == (200, {"symbol": SYMBOL, "marks": []}), (
        "an engine without a state folder (replay)"
    )


async def test_a_bad_query_or_an_unknown_symbol_is_refused(tmp_path: Path) -> None:
    for query, status in (("?limit=0", 400), ("?limit=1001", 400), ("?before=10:00", 400)):
        answered, body = await marks(tmp_path, None, query)
        assert answered == status
        check("error", body)
    answered, body = await marks(tmp_path, tmp_path, symbol="999999")
    assert answered == 404
    check("error", body)


async def test_each_mark_carries_its_trade_s_policy_and_only_the_champion_s_are_marked(
    tmp_path: Path,
) -> None:
    """#399: a trade entered before the first champion (no policy), then policy 1 the champion from
    22:31 on, its trade two mornings later; policy 2, a challenger, trades beside it (#400 runs
    them): its paper trade is not the champion's and is not marked."""
    store = TraderStore(tmp_path / DB_NAME)
    try:
        trade(store, "paper", at("10:00:00"), at("10:02:00"), 1)
        db = policies.connect(tmp_path / DB_NAME)
        try:
            policies.add(db, at("22:31:00"), "밤 맞추기", [], [], {})  # the champion
            policies.add(db, at("22:31:00") + timedelta(days=1), "밤 맞추기", [], [], {})  # a challenger
        finally:
            db.close()
        morning = at("10:00:00") + timedelta(days=2)
        trade(store, "paper", morning, morning + timedelta(minutes=2), 2, policy_id=1)
        trade(store, "paper", morning + timedelta(minutes=1), None, 3, policy_id=2)
    finally:
        store.close()

    status, body = await marks(tmp_path, tmp_path)

    assert status == 200
    check("trader_marks", body)
    assert [(m["trade_id"], m["side"], m["policy"]) for m in body["marks"]] == [
        (2, "sell", 1),
        (2, "buy", 1),
        (1, "sell", None),
        (1, "buy", None),
    ], "the challenger's trade 3 is not marked"


async def test_a_store_from_before_the_policies_marks_every_trade(tmp_path: Path) -> None:
    """The Engine API may read the store before the trader, restarted with #399, adds its columns."""
    Day(tmp_path).run(DAY)
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.execute("DROP TABLE champions")
        db.execute("ALTER TABLE trades DROP COLUMN policy_id")
        db.commit()

    _, body = await marks(tmp_path, tmp_path)

    assert len(body["marks"]) == 5 and {m["policy"] for m in body["marks"]} == {None}
