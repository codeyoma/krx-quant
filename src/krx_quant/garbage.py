"""The live engine's garbage collection, kept off the bar closes (#621).

CPython's collector passes over its oldest generation when the objects that survived into it grew
by a quarter since its last such pass, and that pass walks every object the process keeps while it
holds the GIL: the event loop and the algorithms' threads wait for it. During the session the
engine keeps mostly its quote logs: the Market View's book updates (``market_view._Book``) and the
kinds' hour of quotes (``kinds.QuoteLog``), about 1.2 million named tuples in #600's busy morning
(RSS 650 MB). CPython stops tracking a plain tuple of numbers, never a named tuple, so each pass
walks them all. On GitHub's runners such a pass held the loop up to 0.66 s, twelve times a morning,
and one landed in a bar close in about one morning in five (#600's bench).

``Garbage.settle`` runs at the end of each bar close the engine predicts (``PredictionEngine``'s
``garbage``, the step ``garbage collection``): one full pass over what is not frozen, which is what
the last minute kept (a few milliseconds), then ``gc.freeze``, which moves every object alive into
the permanent generation that no pass walks. The oldest generation then never holds the session's
hour of quotes, and its passes stay short. A frozen object is still freed as soon as nothing refers
to it (the quotes a log forgets): only an object in a reference cycle that was alive at a freeze
and dies later stays, until ``release``, which unfreezes everything and makes one full pass, logged
with how many objects it let go and freed and how long it took. The engine releases once its next
work is over ``IDLE`` away (after the session's last answers), and the collector when it stops.

Measured on GitHub's runners, 6 mornings against 5 (#621): the oldest generation's passes from
0.41-0.66 s at 10:00-10:31 to 2-6 ms each after a bar close (the day's first, over everything
loaded at start, 0.04-0.08 s), the peak RSS 654-661 MB against 661-669 MB.
"""

from __future__ import annotations

import gc
import logging
import time
from datetime import timedelta

log = logging.getLogger(__name__)

# The engine's next work at least this far away: the frozen objects are let go (``release``). The
# session's bar closes and answers come a minute apart; this is past them.
IDLE = timedelta(minutes=10)


class Garbage:
    """The garbage collection of one live engine: a freeze after each bar close, and the release."""

    def __init__(self) -> None:
        self.frozen = False

    def settle(self) -> None:
        """After a bar close: a full pass over what is not frozen, then everything alive frozen."""
        gc.collect()
        gc.freeze()
        self.frozen = True

    def release(self) -> None:
        """Let the frozen objects go and pass over everything once; nothing if none are frozen."""
        if not self.frozen:
            return
        self.frozen = False
        started = time.perf_counter()
        kept = gc.get_freeze_count()
        gc.unfreeze()
        freed = gc.collect()
        log.info(
            "garbage collection: let go of %d frozen objects; a full pass freed %d in %.2fs",
            kept,
            freed,
            time.perf_counter() - started,
        )
