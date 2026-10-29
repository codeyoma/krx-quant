"""The trader (#393, #394): trades 005930 on one cell's predictions by the triple barrier, as its
own process (``krx-quant trade``, compose service ``trader``), apart from the collector.

The first stem (#394) is paper only: the trader fills its orders on the collector's live best
quotes (``quotes.py``) and writes what it did to its own store, ``state/trader.sqlite3``
(``store.py``). It has no order code and calls no KIS order: the KIS order calls live in
``krx_quant.kis_orders`` alone, the one file the repository's forbidden-pattern check
(``scripts/check_forbidden.py``) lets hold KIS order TR IDs and endpoints (#403), and only the
order probe uses them so far; the trader will through its KIS order module (#404).

- **Cell** (``cells.py``): the one (kind, predictor) traded, set in ``.env`` (``settings.py``):
  the four-minute direction (width k) or the 4-, 15- or 30-minute barrier (width w). The trader
  reads the engine's database (``state/engine.sqlite3``) read-only for its predictions as they
  are displayed (``displayed_at``).
- **Rules** (``paper.py``): a prediction of the rise (up; the barrier's up_first) at or above
  the threshold buys, when nothing is held. Its reference price is the prediction's ``anchor``,
  the target ``anchor + w × tick``, the stop ``anchor − w × tick`` (w = k for the direction) and
  the time barrier the bar close plus the kind's horizon. Entries from five minutes after the
  open (09:05), and only when the time barrier is by five minutes before the closing auction
  (15:15); no buy when the best ask has reached the target already.
- **Paper fills**: the buy at the best ask of the first quote received after the decision; the
  take-profit at the target once a best bid reaches it; the stop and the time exit at the best
  bid then. Costs: the commission on both sides and the sell tax. The quantity is the capital
  (1,000,000 won) over the best ask, rounded down.
- **Store** (``store.py``): its decisions, orders, fills and trades, each with its mode
  (``paper``); the Engine API reads it (``GET /v1/trader/trades``).
"""

from __future__ import annotations

from datetime import timedelta

# The one symbol traded (#393).
SYMBOL = "005930"

# The modes (#393): paper, filled on the live quotes inside the trader. KIS's mock and the live
# account (mock, live) come with the KIS order module (#404, #405, #408).
PAPER = "paper"
MODES = (PAPER,)
LATER_MODES = ("mock", "live")

# The kinds a cell may be of (#393): the four-minute direction, and the barrier at 4, 15 and 30
# minutes. The 60-minute barrier and the daily kinds are not traded.
TRADED_KINDS = ("direction", "barrier", "barrier_15m", "barrier_30m")

# The capital the trader may use, in won (#393): ``.env`` may narrow it, never widen it.
CAPITAL = 1_000_000
# The default threshold: a rise's probability at least this buys (the signal alerts' default).
THRESHOLD = 0.6

# Continuous trading only (#393): entries from this long after the open (09:05), and only when
# the time barrier is at least this long before the closing auction (15:15).
ENTRY_AFTER_OPEN = timedelta(minutes=5)
EXIT_BEFORE_AUCTION = timedelta(minutes=5)
