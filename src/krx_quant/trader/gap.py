"""The mock book against its paper shadow (#405): what KIS's execution changed.

In mock mode the champion's trades are its KIS mock fills (``mock.py``, mode ``mock``) and its
paper shadow fills the same policy's trades in paper on the same quotes (``desk.Desk.shadow``,
mode ``paper``, ``trades.shadow`` 1). Both books decide on the same predictions by the same rules,
so the two trades of one prediction differ by execution alone: KIS's fill prices (the mock fills
half a tick better than the quote, #403), a partial fill, a buy that filled nothing, a failed
order; and by what they hold apart (the shadow is held by no limit nor pause, and sizes on the
paper account).

In live mode (#408) the champion's live trades (mode ``live``) have their paper shadow the same way;
``pairs`` gives the two trades of each prediction, the live mode's execution checks and its first
period measure their legs (``live.py``).

``day_gap`` gives a trading day's: each side's trades entered that day and the net of those
exited, the difference (mock − shadow), and each prediction's pair: the entry's and the exit's
fill price difference in ticks of the trade's tick (the mock's less the shadow's: above 0 the mock
paid more on the buy, got more on the sale), the quantities, the exit reasons and the net
difference; a prediction one side traded alone has the other ``null``. ``GET /v1/trader/days``
gives it on each mock day (``gap``), the nightly report prints it. No price, only ticks and won.

**Books** (#427). The mock book's practice trades (``store.PRACTICE_TRADE``) have their own paper
shadow (``desk.Desk.practice_shadow``): a pair is of one book, its key the book (practice or not)
and the prediction, so that a practice trade never pairs with the champion policy's shadow nor a
champion trade with the practice shadow. Every trade of the day is in exactly one pair
(``pairing``): a key that one side holds more than once (it should not: a book decides a prediction
once) pairs none of them, each one-sided, so that no trade is dropped unseen; ``Pairing.alone``
counts the one-sided, which the live mode's gap check says (``live.py``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from krx_quant.trader import MOCK, PAPER

_COLUMNS = (
    "kind, predictor, prediction_id, bar_close, tick, entry_at, entry_price, quantity, exit_price,"
    " exit_reason, net, {practice} AS practice"
)

Key = tuple[bool, str, str, int, str]  # the book (practice or not), then the prediction


def _trades(db: sqlite3.Connection, mode: str, days: Iterable[date]) -> tuple[list[Any], list[Any]]:
    """``mode``'s champion trades entered on ``days`` (the mock book's practice trades among them,
    #427) and the paper shadows' (the champion policy's and the practice one's), oldest first."""
    wanted = sorted({d.isoformat() for d in days})
    columns = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    if "shadow" not in columns or not wanted:
        return [], []
    # A store the trader has not opened since #427 holds no practice trade.
    picked = _COLUMNS.format(practice="coalesce(practice, 0)" if "practice" in columns else "0")
    marks = ",".join("?" * len(wanted))
    kis = db.execute(
        f"SELECT {picked} FROM trades t WHERE mode = ? AND coalesce(champion, 1) = 1"
        f" AND substr(entry_at, 1, 10) IN ({marks}) ORDER BY entry_at, id",
        (mode, *wanted),
    ).fetchall()
    shadow = db.execute(
        f"SELECT {picked} FROM trades t WHERE mode = ? AND coalesce(shadow, 0) = 1"
        f" AND substr(entry_at, 1, 10) IN ({marks}) ORDER BY entry_at, id",
        (PAPER, *wanted),
    ).fetchall()
    return kis, shadow


@dataclass(frozen=True)
class Pairing:
    """Every trade of the KIS book and of the shadows on some days, each once (module doc): the
    pairs both books traded (``pairs``: the KIS trade, the shadow's) and the trades one side traded
    alone (``alone``: the other side None), in the keys' order."""

    pairs: tuple[tuple[Any, Any], ...]
    alone: tuple[tuple[Any | None, Any | None], ...]

    @property
    def kis_alone(self) -> int:
        """The KIS trades without a shadow's to pair with."""
        return sum(1 for kis, _ in self.alone if kis is not None)


def pairing(db: sqlite3.Connection, mode: str, days: Iterable[date]) -> Pairing:
    """The champion's ``mode`` trades (mock, live) entered on ``days`` against the paper shadows',
    each with its book's shadow trade of the same prediction (module doc). Each row has its
    ``tick``, ``entry_price``, ``exit_price`` (null while open), ``quantity``, ``net`` and
    ``practice``."""
    kis, shadow = _trades(db, mode, days)
    return _pair_up(kis, shadow)


def _pair_up(kis: list[Any], shadow: list[Any]) -> Pairing:
    by_kis: dict[Key, list[Any]] = {}
    by_shadow: dict[Key, list[Any]] = {}
    for rows, found in ((kis, by_kis), (shadow, by_shadow)):
        for r in rows:
            found.setdefault(_key(r), []).append(r)
    paired: list[tuple[Any, Any]] = []
    alone: list[tuple[Any | None, Any | None]] = []
    for key in sorted(set(by_kis) | set(by_shadow), key=_order):
        mine, theirs = by_kis.get(key, []), by_shadow.get(key, [])
        if len(mine) == 1 and len(theirs) == 1:
            paired.append((mine[0], theirs[0]))
            continue
        alone += [(r, None) for r in mine] + [(None, r) for r in theirs]
    assert len(paired) * 2 + len(alone) == len(kis) + len(shadow), "every trade in exactly one pair"
    return Pairing(tuple(paired), tuple(alone))


def pairs(db: sqlite3.Connection, mode: str, days: Iterable[date]) -> list[tuple[Any, Any]]:
    """The champion's ``mode`` trades (mock, live) entered on ``days``, each with its book's paper
    shadow trade of the same prediction: those both books traded, oldest first (``pairing``)."""
    return list(pairing(db, mode, days).pairs)


def day_gap(db: sqlite3.Connection, day: date, mode: str = MOCK) -> dict[str, Any] | None:
    """``day``'s ``mode`` trades (mock; live, #408) against the shadows' (module doc); None on a day
    with neither."""
    mock, shadow = _trades(db, mode, [day])
    if not mock and not shadow:
        return None
    found = _pair_up(mock, shadow)
    sides = [*found.pairs, *found.alone]
    sides.sort(key=lambda pair: _order(_key(pair[0] if pair[0] is not None else pair[1])))
    mock_net, shadow_net = _net(mock), _net(shadow)
    return {
        "mock": {"trades": len(mock), "net": mock_net},
        "shadow": {"trades": len(shadow), "net": shadow_net},
        "difference": mock_net - shadow_net,
        "pairs": [_pair(m, s) for m, s in sides],
    }


def _key(row: sqlite3.Row) -> Key:
    return bool(row["practice"]), row["kind"], row["predictor"], row["prediction_id"], row["bar_close"]


def _order(key: Key) -> tuple[str, str, str, int, bool]:
    """Pairs in the order of their bar close, then their cell and prediction, then their book."""
    practice, kind, predictor, prediction, close = key
    return close, kind, predictor, prediction, practice


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
        "practice": bool(some["practice"]),  # the mock book's practice trade and its shadow (#427)
        "mock": _side(mock),
        "shadow": _side(shadow),
        "entry_ticks": entry,
        "exit_ticks": exit_,
        "net_difference": net,
    }
