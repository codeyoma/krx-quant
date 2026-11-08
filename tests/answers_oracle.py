"""The answers' code as it was before #630, kept as the oracle the faster code is checked against
(``tests/test_answers_equivalence.py``):

- a Market View that hands every quote and trade to every touch window it keeps, each window
  dropping what falls outside its four minutes, and looks for a window open now by scanning them
  all (``market_view.MarketView._follow``);
- the barrier answer that finds each barrier's first reach by the earliest received of all the
  quotes that reached it (``barriers.barrier_answer``, ``barriers._first``);
- the silence test that walks the received times one by one (``kinds.went_silent``), and the
  window's quotes taken for a kind with it (``kinds.follow``).

Copied from the commit before #630, unchanged but for the names and the docstrings. Nothing in
``src/`` reads it. ``before_630`` puts it in place of today's code for a block.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest

from krx_quant import barriers, kinds, market_view, ranges, training
from krx_quant.barriers import AMBIGUOUS, DOWN_FIRST, TIMEOUT, UP_FIRST, _at
from krx_quant.features import Width
from krx_quant.feeds import BOOK_SNAPSHOT, KRX_TRADE, TRADE
from krx_quant.kinds import Answer, Followed, Kind, Quote, Taken
from krx_quant.market_view import MarketView
from krx_quant.touch import MISSING_MID, REST_QUOTES, SILENCE, SILENT, WINDOW


class OracleView(MarketView):
    """The Market View with the touch windows followed as before #630."""

    def _follow(self, feed: str, symbol: str, received_at: datetime, row: dict[str, Any]) -> None:
        if feed == KRX_TRADE.tr_id or self._fee_rate is None:
            return  # the KRX-only quotes before NXT opens: not the integrated book
        rest = feed == BOOK_SNAPSHOT.tr_id
        if feed == TRADE.tr_id:
            bid, ask, bid_size, ask_size = row["bid1"], row["ask1"], None, None
        else:
            bid, ask = row["bid_px_1"], row["ask_px_1"]
            bid_size, ask_size = row.get("bid_qty_1"), row.get("ask_qty_1")
        if not rest and bid and ask and 0 < bid <= ask:
            self._best[symbol] = (float(bid), float(ask))
        for window in self._windows[symbol].values():
            window.quote(received_at, bid, ask, bid_size=bid_size, ask_size=ask_size, rest=rest)
            if feed == TRADE.tr_id:
                window.trade(received_at, row["price"])
        log = self._logs.get(symbol)
        if log is not None and any(c <= received_at < c + self._horizon for c in self._windows[symbol]):
            log.quote(received_at, bid, ask, bid_size, ask_size, rest)
            if feed == TRADE.tr_id:
                log.trade(received_at, row["price"])


def went_silent(start: datetime, end: datetime, received: Sequence[datetime]) -> bool:
    last = start
    for at in received:
        if at - last >= SILENCE:
            return True
        last = at
    return end - last >= SILENCE


def follow(
    taken: Taken,
    symbol: str,
    bar_close: datetime,
    anchor: float | None,
    tick: float | None,
    k: int | None,
    bid_start: float | None,
    ask_start: float | None,
    *,
    cost: float | None = None,
    width: Width | None = None,
    crossed: bool = False,
    horizon: timedelta = WINDOW,
) -> Followed:
    quotes = tuple(taken.quotes) if crossed else tuple(q for q in taken.quotes if not q.crossed)
    silent = went_silent(bar_close, bar_close + horizon, [q.received_at for q in quotes])
    unscorable = MISSING_MID if anchor is None else REST_QUOTES if taken.rest else SILENT if silent else None
    return Followed(
        symbol,
        bar_close,
        anchor,
        tick,
        k,
        bid_start,
        ask_start,
        quotes,
        tuple(taken.trades),
        unscorable,
        cost,
        width,
        horizon,
    )


def _first(quotes: Sequence[Quote], reached: Any) -> Quote | None:
    return min((q for q in quotes if reached(q)), key=lambda q: q.received_at, default=None)


def barrier_answer(window: Followed) -> Answer:
    width, anchor, tick = window.width, window.anchor, window.tick
    upper = lower = None
    if width is not None and anchor is not None and tick is not None:
        upper, lower = anchor + width.ticks * tick, anchor - width.ticks * tick
    up = _first(window.quotes, lambda q: q.bid >= upper) if upper is not None else None
    down = _first(window.quotes, lambda q: q.ask <= lower) if lower is not None else None
    last = window.quotes[-1] if window.quotes else None
    path: dict[str, Any] = {
        "w": width.ticks if width is not None else None,
        "sigma": width.sigma if width is not None else None,
        "k": window.k,
        "tick": tick,
        "anchor": anchor,
        "upper": upper,
        "lower": lower,
        "cost": window.cost,
        "bid_start": window.bid_start,
        "ask_start": window.ask_start,
        "up_at": _at(up.received_at) if up else None,
        "up_bid": up.bid if up else None,
        "up_ask": up.ask if up else None,
        "down_at": _at(down.received_at) if down else None,
        "down_bid": down.bid if down else None,
        "down_ask": down.ask if down else None,
        "bid_end": last.bid if last else None,
        "ask_end": last.ask if last else None,
        "quotes": len(window.quotes),
        "silent": went_silent(window.bar_close, window.end, [q.received_at for q in window.quotes]),
    }
    if upper is None:
        return Answer(None, None, path)
    if up is not None and down is not None and up.received_at == down.received_at:
        return Answer(None, AMBIGUOUS, path)
    if up is not None and (down is None or up.received_at < down.received_at):
        return Answer(UP_FIRST, None, path)
    return Answer(DOWN_FIRST if down is not None else TIMEOUT, None, path)


def oracle_kinds(given: Sequence[Kind]) -> tuple[Kind, ...]:
    """``given`` with the barrier answer as it was before #630: a kind holds its answer function."""
    return tuple(
        dataclasses.replace(k, answer=barrier_answer) if k.answer is barriers.barrier_answer else k
        for k in given
    )


@contextmanager
def before_630(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The code before #630 in place of today's while the block runs (give it ``oracle_kinds``)."""
    with monkeypatch.context() as patched:
        patched.setattr(training, "MarketView", OracleView)
        patched.setattr(market_view, "follow", follow)
        patched.setattr(kinds, "follow", follow)
        for module in (kinds, ranges, barriers):
            patched.setattr(module, "went_silent", went_silent)
        yield
