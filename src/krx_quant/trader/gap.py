"""The mock book against its paper shadow (#405): what KIS's execution changed.

In mock mode the champion's trades are its KIS mock fills (``mock.py``, mode ``mock``) and its
paper shadow fills the same policy's trades in paper on the same quotes (``desk.Desk.shadow``,
mode ``paper``, ``trades.shadow`` 1). Both books decide on the same predictions by the same rules,
so the two trades of one prediction differ by execution alone: KIS's fill prices (the mock fills
half a tick better than the quote, #403), a partial fill, a buy that filled nothing, a failed
order; and by what they hold apart (the shadow is held by no limit nor pause, and sizes on the
paper account).

``day_gap`` gives a trading day's: each side's trades entered that day and the net of those
exited, the difference (mock − shadow), and each prediction's pair: the entry's and the exit's
fill price difference in ticks of the trade's tick (the mock's less the shadow's: above 0 the mock
paid more on the buy, got more on the sale), the quantities, the exit reasons and the net
difference; a prediction one side traded alone has the other ``null``. ``GET /v1/trader/days``
gives it on each mock day (``gap``), the nightly report prints it. No price, only ticks and won.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from krx_quant.trader import MOCK, PAPER

_COLUMNS = (
    "kind, predictor, prediction_id, bar_close, tick, entry_price, quantity, exit_price, exit_reason, net"
)


def day_gap(db: sqlite3.Connection, day: date) -> dict[str, Any] | None:
    """``day``'s mock trades against the shadow's (module doc); None on a day with neither."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    if "shadow" not in columns:
        return None
    mock = db.execute(
        f"SELECT {_COLUMNS} FROM trades t WHERE mode = ? AND coalesce(champion, 1) = 1"
        " AND substr(entry_at, 1, 10) = ? ORDER BY entry_at, id",
        (MOCK, day.isoformat()),
    ).fetchall()
    shadow = db.execute(
        f"SELECT {_COLUMNS} FROM trades t WHERE mode = ? AND coalesce(shadow, 0) = 1"
        " AND substr(entry_at, 1, 10) = ? ORDER BY entry_at, id",
        (PAPER, day.isoformat()),
    ).fetchall()
    if not mock and not shadow:
        return None
    by_mock = {_key(r): r for r in mock}
    by_shadow = {_key(r): r for r in shadow}
    keys = sorted(set(by_mock) | set(by_shadow), key=lambda k: (k[3], k[0], k[1], k[2]))
    pairs = [_pair(by_mock.get(k), by_shadow.get(k)) for k in keys]
    mock_net, shadow_net = _net(mock), _net(shadow)
    return {
        "mock": {"trades": len(mock), "net": mock_net},
        "shadow": {"trades": len(shadow), "net": shadow_net},
        "difference": mock_net - shadow_net,
        "pairs": pairs,
    }


def _key(row: sqlite3.Row) -> tuple[str, str, int, str]:
    return row["kind"], row["predictor"], row["prediction_id"], row["bar_close"]


def _net(rows: list[sqlite3.Row]) -> float:
    return float(sum(r["net"] for r in rows if r["net"] is not None))


def _side(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"quantity": row["quantity"], "exit_reason": row["exit_reason"], "net": row["net"]}


def _pair(mock: sqlite3.Row | None, shadow: sqlite3.Row | None) -> dict[str, Any]:
    some = mock if mock is not None else shadow
    assert some is not None
    entry = exit_ = net = None
    if mock is not None and shadow is not None:
        tick = mock["tick"] or shadow["tick"]
        entry = (mock["entry_price"] - shadow["entry_price"]) / tick
        if mock["exit_price"] is not None and shadow["exit_price"] is not None:
            exit_ = (mock["exit_price"] - shadow["exit_price"]) / tick
        if mock["net"] is not None and shadow["net"] is not None:
            net = mock["net"] - shadow["net"]
    return {
        "kind": some["kind"],
        "predictor": some["predictor"],
        "bar_close": some["bar_close"],
        "mock": _side(mock),
        "shadow": _side(shadow),
        "entry_ticks": entry,
        "exit_ticks": exit_,
        "net_difference": net,
    }
