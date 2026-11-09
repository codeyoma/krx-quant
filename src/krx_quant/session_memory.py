"""What the live engine's Market View keeps of a session, and letting it go once it is over (#623).

A bar close's snapshot reads the book updates, the trades and the day's flows received from its
session's open on (``MarketView.order_flow``, ``microprices``, ``arrivals``, ``flows``), and a
window's answer the quotes logged from its bar close on. Each of the view's logs forgets what is
older only as it is added to, so what a session ends with stayed until the next one's first events:
the last hour or two of book updates and of the kinds' quotes, the day's trades and flows, through
the evening and the night, when the nightly training needs the server's memory. ``let_go`` drops
them once the session's windows are all answered, exactly what each log would drop at the next
session's first events; ``held`` counts what the view keeps, for the line the engine's rest says
(``garbage.Garbage.rest``).

This lives apart from ``market_view.py`` and reaches into the view's logs from outside, because
when it was written that module, ``kinds.py`` and ``reference.py`` were in the key of every day's
stored samples whole: a change to their code had the night make every day again, hours of it. Since
#645 the key holds what the samples mean, not the code, and moving this into the view is #654.
Nothing here changes what a sample or a prediction is made of (``tests/test_session_memory.py``:
two days through an engine that lets go and one that does not read and store the same). A change
to how those logs keep their entries is a change here too.
"""

from __future__ import annotations

import bisect
from collections.abc import Collection
from datetime import datetime, timedelta

from krx_quant.feeds.base import kst_day
from krx_quant.market_view import MarketView, _Arrivals, _Book, _Estimates, _Flow, _Running
from krx_quant.touch import WINDOW

# What the view lets go of, as ``held``, ``let_go`` and the rest's line name them: each symbol's
# book updates, the kinds' logged quotes and trades, the day's trades by aggressor, the running
# totals of the trades' sides and venues, the day's flows (program trading, foreign member firms,
# investor estimates).
BOOK_UPDATES, KIND_QUOTES, TRADES, TRADE_SIDES, FLOWS = (
    "book updates",
    "kind quotes",
    "trades",
    "trade sides",
    "flows",
)
LET_GO = (BOOK_UPDATES, KIND_QUOTES, TRADES, TRADE_SIDES, FLOWS)


def held(view: MarketView) -> dict[str, int]:
    """How much ``view`` keeps, by what it is: the entries ``let_go`` drops, then the reference
    prices' quotes, the day's bars and the touch windows, which it leaves."""
    return {
        BOOK_UPDATES: sum(len(b._at) for b in view._books.values()),
        KIND_QUOTES: sum(len(q._quotes) + len(q._rests) + len(q._trades) for q in view._logs.values()),
        TRADES: sum(len(a._at) for a in view._arrivals.values()),
        TRADE_SIDES: sum(len(r._at) for r in (*view._sides.values(), *view._venues.values())),
        FLOWS: sum(len(f._at) for f in (*view._program.values(), *view._foreign_members.values()))
        + sum(len(e._rows) for e in view._estimates.values()),
        "reference quotes": sum(len(q.times) for q in view._reference._quotes.values()),
        "bars": sum(len(b) for builder in (view._bars, view._leader_bars) for b in builder._bars.values()),
        "touch windows": sum(len(w) for w in view._windows.values()),
    }


def let_go(view: MarketView, opened: datetime, horizons: Collection[timedelta]) -> dict[str, int]:
    """Drop from ``view`` what no bar close of a session that opens at ``opened`` or later reads:
    the book updates (each symbol's last stays), the trades' sides and venues and the kinds'
    quotes received before ``opened``, and the trades and flows of the days before its day. How
    many of each went (``LET_GO``'s names); nothing, and none dropped, while a window followed is
    still to be answered at one of ``horizons`` (the kinds'; the touch windows' four minutes
    always): its kinds read the quotes logged.

    The reference prices' quotes stay (a late look back reads the last minutes: the 15:20 closing
    demand), as do the bars and the touch windows (the day's chart, a late score)."""
    if _unanswered(view, {WINDOW, *horizons}):
        return {}
    kind_quotes = held(view)[KIND_QUOTES]
    for log in view._logs.values():
        log.forget(opened)
    flows = [*view._program.values(), *view._foreign_members.values()]
    return {
        BOOK_UPDATES: sum(_forget_book(b, opened) for b in view._books.values()),
        KIND_QUOTES: kind_quotes - held(view)[KIND_QUOTES],
        TRADES: sum(_forget_arrivals(a, opened) for a in view._arrivals.values()),
        TRADE_SIDES: sum(_forget_running(r, opened) for r in (*view._sides.values(), *view._venues.values())),
        FLOWS: sum(_forget_flow(f, opened) for f in flows)
        + sum(_forget_estimates(e, opened) for e in view._estimates.values()),
    }


def _unanswered(view: MarketView, horizons: Collection[timedelta]) -> bool:
    """Whether a window ``view`` follows is still to be handed out (``ended_windows``) at a length
    it is answered at: every touch window, and a longer kind's that ends by the closing auction."""
    closes = {close for windows in view._windows.values() for close in windows}
    for horizon in horizons:
        through = view._ended_through.get(horizon)
        for close in closes:
            if (through is None or close > through) and (
                horizon == WINDOW or view.within_session(close, horizon)
            ):
                return True
    return False


def _forget_running(running: _Running, before: datetime) -> int:
    """Drop what ``running`` received before ``before``, as its ``add`` does what is ``keep`` old:
    a window that starts at ``before`` or later sums the same. How many that dropped."""
    cut = bisect.bisect_left(running._at, before)
    if cut:
        running._dropped = running._totals[cut - 1]
        del running._at[:cut], running._totals[:cut]
    return cut


def _forget_book(book: _Book, before: datetime) -> int:
    """Drop the updates ``book`` received before ``before``, all but the last (the next update's
    order flow is measured from it, ``_Book.add``): nothing asked about ``before`` +
    ``MAX_QUOTE_AGE`` or later reads them. How many that dropped."""
    cut = min(bisect.bisect_left(book._at, before), len(book._at) - 1)
    if cut > 0:
        del book._at[:cut], book._micro[:cut], book._depth[:cut], book._top[:cut]
    _forget_running(book._flow, before)
    return max(cut, 0)


def _forget_arrivals(arrivals: _Arrivals, before: datetime) -> int:
    """Drop the trades of a day earlier than ``before``'s, as that day's first trade will: nothing
    asked about ``before``'s day reads them (``_Arrivals.before``). How many that dropped."""
    if arrivals._day is None or arrivals._day >= kst_day(before):
        return 0
    dropped = len(arrivals._at)
    arrivals._reset()
    return dropped


def _forget_flow(flow: _Flow, before: datetime) -> int:
    """Drop the values of a day earlier than ``before``'s, as that day's first value will
    (``_Flow.before``, ``base``). How many that dropped."""
    if flow._day is None or flow._day >= kst_day(before):
        return 0
    dropped, flow._at, flow._values = len(flow._at), [], []
    return dropped


def _forget_estimates(estimates: _Estimates, before: datetime) -> int:
    """Drop the rows of a day earlier than ``before``'s, as that day's first row will
    (``_Estimates.before``). How many that dropped."""
    if estimates._day is None or estimates._day >= kst_day(before):
        return 0
    dropped, estimates._rows = len(estimates._rows), []
    return dropped
