"""The trader (#393, #394, #395): trades 005930 by the triple barrier on the cells the cost map
passes, as its own process (``krx-quant trade``, compose service ``trader``), apart from the
collector. In live mode (#408, ``live.py``) the champion orders on the new live account, past the
live gate and the execution checks alone.

In paper (``KRX_TRADER_MODE=paper``, the default, #394) the trader fills its orders on the
collector's live best quotes (``quotes.py``) and writes what it did to its own store,
``state/trader.sqlite3`` (``store.py``). In mock (#405, ``mock.py``) the champion's book places its
orders on KIS's mock server instead, through the broker of ``krx_quant.kis_orders``
(``kis_orders.Broker``, #404: orders, cancels, fills read off the day's order inquiry, the
orderable cash, with the trader's own key and token), and writes its trades from KIS's fills; the
challengers stay in paper. The trader holds no order code: the KIS order calls live in
``krx_quant.kis_orders`` alone, the one file the repository's forbidden-pattern check
(``scripts/check_forbidden.py``) lets hold KIS order TR IDs and endpoints (#403).

- **Cells** (``selection.py``, #395): a cell is a (kind, predictor): the four-minute direction
  (width k) or the 4-, 15- or 30-minute barrier (width w), and the final consensus (the
  direction's only), the kind's first-ranked algorithm or its strongest baseline. The trader
  trades the cells whose verdict on the engine's cost map (``GET /v1/cost-map``, 005930, the last
  20 days) is ``pass`` at a market order's round trip, picked at each start and every day at
  08:30; none passing, it trades nothing that day and keeps why. It reads their predictions from
  the engine's database (``state/engine.sqlite3``) read-only as they are displayed
  (``displayed_at``, ``cells.py``).
- **Rules** (``paper.py``): a prediction of the rise (up; the barrier's up_first) at or above the
  threshold (its cell's under the champion policy) buys, when nothing is held and no special
  situation (a VI, a circuit breaker, a sidecar) touches its window. Of the rises of one bar
  close, the cell ranked first (the champion's order; before the first, the greatest hit rate's
  lower bound less p*) buys. Its reference price is the prediction's ``anchor``, the target
  ``anchor + w × tick``, the stop ``anchor − w × tick`` (w = k for the direction) and the time
  barrier the bar close plus the kind's horizon. Entries from five
  minutes after the open (09:05), and only when the time barrier is by five minutes before the
  closing auction (15:15); no buy when the best ask has reached the target already.
- **Size**: the cell's fractional Kelly (λ × f*, from its hit rate's lower bound and its payoff:
  the target less the cost against the stop and the cost), cut to 0–1, of the capital (1,000,000
  won), over the best ask, rounded down; none below one share.
- **Paper fills**: the buy at the best ask of the first quote received after the decision; the
  take-profit at the target once a best bid reaches it; the stop and the time exit at the best
  bid then. Costs: the commission on both sides and the sell tax.
- **Policy** (``policy.py``, ``refit.py``, #399): a versioned set of values: the cells traded and
  their order, each cell's threshold θ and Kelly fraction λ; no limit. Every trading day's night,
  after the nightly training, the scheduled job ``trader-report`` (#402) fits a new version on the
  stored predictions and answers of the cost map's 20 days, simulating the trader's trades on the
  answers' paths at each point of a small grid. The first is the champion, the later ones
  challengers. At each pick the trader trades the champion's cells among those the cost map
  passes, in its order, at its θ and λ; before the first champion, the cost map's cells at
  ``KRX_TRADER_THRESHOLD`` and ``KELLY_FRACTION``.
- **Challengers** (``desk.py``, ``promotion.py``, #400): up to three run in paper beside the
  champion from the next trading day, each in its own book on the same predictions and quotes,
  held by no limit nor pause; trades and decisions are split by policy version. The nightly job
  promotes one that beat the champion on at least ten common trading days (the day-level lower
  bound of challenger − champion net P&L above 0, Bonferroni over the challengers), after the
  market only. The live gate (#408): judged at checkpoints, every 20th day of the trader's paper
  history, on the champion of that day and its paper record of 20 days at least, the day-level
  lower bound of its daily net P&L above 0 at α_k = 0.025 × 6 ÷ (π² k²); a champion that passed
  stays passed.
- **Live** (``live.py``, #408): with ``KRX_TRADER_MODE=live`` the champion's book is the KIS book of
  mode ``live`` on the live account (``kis_orders.trader_live_broker``: the live key alone), once the
  live gate and the execution checks on the mock days pass (KIS days compared, no failed order nor
  reconcile pause, nothing left open, flat by the auction, the adverse gap a leg within bounds, the
  gate passing with one extra tick a leg); until then the trader runs as paper and says what is
  missing. Its first period sizes on 250,000 won until 20 live days and 30 round trips pass their
  checks; a breach pauses new entries until ``krx-quant trade resume``.
- **Limits and pauses** (``limits.py``, #396): the loss is the net realized P&L plus the open
  position's at the best bid, after its costs; a day's of 10% of the capital stops the day (sold
  at once, trading again the next trading day), a cumulative one of 20% stops the trader until
  ``krx-quant trade rearm``. The screen's two pauses (``POST /v1/trader/pause``: new entries, or
  that and a sale now) last until ``krx-quant trade resume`` on the server. They hold the trades
  the trader makes, the champion's: no policy changes them.
- **Report** (``report.py``, #402): the same night's job prints the day's report above the
  re-fit's output (``krx-quant refit-policy --report``), one comment on the report issue, labelled
  ``ready-for-agent`` on the week's last trading day and on the nights of a limit halt, a change
  of the champion, the live gate passed or a failed re-fit; ``krx-quant trader-report`` prints it
  alone, read-only. It holds no price, account, order number nor key.
- **Mock** (``mock.py``, #405): with ``KRX_TRADER_MODE=mock`` the champion's book orders on KIS's
  mock server: a limit buy at the best ask (what is left cancelled after a few seconds, a partial
  fill a position of that), the take-profit's limit sell resting at the target, the stop's and the
  time exit's limit sells at the best bid; its trades written from KIS's fills. A failed order
  stops new entries for the day. Beside it the champion policy's paper shadow keeps its paper
  record (``desk.Desk.shadow``): every record is paper's in every mode, and ``gap.py`` shows the
  KIS trades against the shadow's. ``mock_check.py`` compares a mock day's orders with KIS's.
- **Quotes stalled** (``stall.py``, #407): more than five seconds without a quote in continuous
  trading is a stall, kept and sent to Telegram with the quotes' return, once each: no book buys
  anything new meanwhile; the mock champion keeps its open position's stop and time barrier on KIS's
  best quotes read over REST on the trader's key (every two seconds, within its call limit), and a
  paper book waits for the stream and keeps that wait on its trade.
- **Store** (``store.py``): its picks of the cells, decisions, orders, fills and trades, each with
  its mode (``paper``, ``mock``) and its book's policy, the policy versions and the days each ran,
  the pauses, resumes and re-arms asked for, the halts, the failed orders, the trader's state now
  and each day's ledger; the Engine API reads it
  (``GET /v1/trader``, ``/v1/trader/trades``, ``/v1/trader/days``, ``/v1/trader/policies``) and
  writes a pause into it (``POST /v1/trader/pause``), nothing else.
"""

from __future__ import annotations

from datetime import time, timedelta

# The one symbol traded (#393).
SYMBOL = "005930"

# The modes (#393): paper, filled on the live quotes inside the trader; mock (#405), the champion's
# orders placed on KIS's mock server (``mock.py``), the challengers in paper beside it; live (#408),
# the champion's orders placed on the live account the same way, once the live gate and the execution
# checks pass (``live.py``; until then the trader runs as paper). The two that order on KIS
# (``KIS_MODES``) share the KIS book (``mock.MockTrader``).
PAPER = "paper"
MOCK = "mock"
LIVE = "live"
MODES = (PAPER, MOCK, LIVE)
KIS_MODES = (MOCK, LIVE)

# The kinds a cell may be of (#393): the four-minute direction, and the barrier at 4, 15 and 30
# minutes. The 60-minute barrier and the daily kinds are not traded.
TRADED_KINDS = ("direction", "barrier", "barrier_15m", "barrier_30m")
# A cell's predictor, as the cost map names it (#307, #395): the final consensus (the direction's
# only), the kind's first-ranked algorithm besides the consensus, its strongest baseline.
ROLES = ("consensus", "top", "baseline")

# The capital the trader may use, in won (#393): ``.env`` may narrow it, never widen it.
CAPITAL = 1_000_000
# The loss limits (#393 "한도와 정지", #396), in percent of the capital: a day's loss of 10%
# (100,000 won) stops the day, a cumulative loss of 20% (200,000 won) stops the trader until it is
# re-armed on the server (``krx-quant trade rearm``). ``.env`` may narrow them, never widen them,
# and the policy never touches them (``limits.py``).
DAILY_LOSS_PERCENT = 10.0
CUMULATIVE_LOSS_PERCENT = 20.0
# The default threshold: a rise's probability at least this buys (the signal alerts' default);
# until the first champion policy, whose θ for the cell it is from then on (#399).
THRESHOLD = 0.6
# The share of a cell's Kelly fraction bought (#395): half Kelly, cut to 0–1 after; until the
# first champion policy, whose λ for the cell it is from then on (#399).
KELLY_FRACTION = 0.5

# The cost map the cells are picked from (#395): 005930's, its last 20 days with predictions, the
# special situations left out (the trader buys in none). Picked at each start, and every day at
# PICK_AT: after the night (the training, the scheduled jobs, the 07:20 restart and the 08:00
# model swap), before the open.
COST_MAP_PERIOD = "20d"
PICK_AT = time(8, 30)

# Continuous trading only (#393): entries from this long after the open (09:05), and only when
# the time barrier is at least this long before the closing auction (15:15).
ENTRY_AFTER_OPEN = timedelta(minutes=5)
EXIT_BEFORE_AUCTION = timedelta(minutes=5)
