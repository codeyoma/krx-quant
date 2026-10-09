"""Algorithm lifecycle: each algorithm's status, changed by rules after each day's close.

- **probation**: new. After 10 trading days, it is made active if the lower bound of its
  weakest lift over the baselines (all its probation days) is above 0.
- **active**: in the consensus (#18). Benched if its weakest lift over the last 20
  trading days is 0 or less.
- **bench**: still predicted and scored. Made active again if its weakest lift over the
  last 20 trading days has a lower bound above 0. After 60 trading days on the bench
  without that, it is a retirement candidate: an alert is sent, once.
- **retired**: by command only (``krx-quant retire``); it is no longer run. Its records
  stay. ``krx-quant unretire`` puts it back on probation.
- **clone**: an algorithm on probation whose predictions move with an earlier registered
  one's (correlation of p_up − p_down above 0.9, on the bar closes both predicted) is a
  clone, kept out of the consensus; back on probation once the correlation falls.

The statistics are the leaderboard's: basis B, special predictions left out, and a
decision only with at least ``min_sample`` scored predictions and as many pairs with each
baseline; with fewer, the status stays. A trading day counts when the engine predicted
on it, from the day the status began.
"""

from __future__ import annotations

import enum


class Status(enum.Enum):
    PROBATION = "probation"
    ACTIVE = "active"
    BENCH = "bench"
    RETIRED = "retired"
    CLONE = "clone"
