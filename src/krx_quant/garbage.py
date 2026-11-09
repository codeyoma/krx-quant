"""The live engine's garbage collection, kept off the bar closes (#621), and its memory handed back
once the session is over (#623).

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
with how many objects it let go and freed and how long it took. The engine releases when it rests
(below: after the session's last answers and after the daily review), and the collector when it
stops.

Measured on GitHub's runners, 6 mornings against 5 (#621): the oldest generation's passes from
0.41-0.66 s at 10:00-10:31 to 2-6 ms each after a bar close (the day's first, over everything
loaded at start, 0.04-0.08 s), the peak RSS 654-661 MB against 661-669 MB.

That release gave no memory back: the session's quote logs were still referred to, and stayed
through the evening and the night, when the nightly training needs the server's memory (#623).
``Garbage.rest`` is what the engine does instead, twice a trading day: once its next work is over
``IDLE`` away after a bar close (after the session's last answers) and after each daily review. It
lets go of what the session kept (the engine's ``let_go``: ``MarketView.let_go``), releases, hands
the freed memory back to the system (``footprint.hand_back``: Arrow's pool, glibc's
``malloc_trim``) and freezes again what stays, so that the passes the evening's work sets off (the
review's rows, the Engine API's) do not walk it either. One INFO line says what went and what the
process holds (``memory handed back …``).

What that gives back, measured on a made-up day through the engine in the image (``tests/
bar_close_bench.py day``, 40 earlier days' predictions for the review to read, twice each at the
bench's busy rate and at a quarter of it, which grows the engine about as much as the server's
session did on 2026-11-06): the resident memory after the review from 911-917 MB to 727-736 MB
and from 466-471 MB to 319-323 MB, and the longest the review kept the event loop without a turn
(a full pass each time) from 0.83-0.85 s and 0.30-0.32 s to 0.06-0.07 s. Not to what the engine
started with (142 MB): Python keeps objects under 512 bytes in 1 MB arenas and returns an arena
only when all of it is free, and the few objects of the session that stay (the day's bars, the
touch windows, the reference prices' last minutes) are spread over nearly all of them. The freed
blocks are used again by what comes next, which is why the review hardly grows the process any
more, but only a new process gives them back.
"""

from __future__ import annotations

import gc
import logging
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from krx_quant import footprint

log = logging.getLogger(__name__)

# The engine's next work at least this far away after a bar close: it rests (``rest``, which lets
# the frozen objects go). The session's bar closes and answers come a minute apart; this is past
# them.
IDLE = timedelta(minutes=10)
# The ``runs`` kind of a rest (#623): its detail is JSON, ``Garbage.rest``'s figures. For the night's
# report of the memory after the close (``session_memory_probe.py``), which cannot read Loki.
REST_RUN = "memory_rest"


class Garbage:
    """The garbage collection of one live engine: a freeze after each bar close, the release, and
    the rest once the session is over."""

    def __init__(self) -> None:
        self.frozen = False
        self._settled = False  # a bar close since the last rest

    def settle(self) -> None:
        """After a bar close: a full pass over what is not frozen, then everything alive frozen."""
        gc.collect()
        gc.freeze()
        self.frozen = True
        self._settled = True

    def rest(
        self,
        let_go: Callable[[], Mapping[str, int]],
        held: Callable[[], Mapping[str, int]],
        *,
        reviewed: bool,
    ) -> dict[str, Any] | None:
        """Out of the session (#623): ``let_go`` drops what the session kept and says how many of
        each; then the frozen are released, the memory freed is handed back to the system, and
        what stays is frozen again. One INFO line says what went, what ``held`` counts now, and
        the process's memory before and after; the same figures are returned, for the engine to
        keep (``REST_RUN``). Nothing, and None, when no bar close came since the last rest and no
        review ran (``reviewed``): there is nothing new to give back."""
        if not (self._settled or reviewed):
            return None
        started = time.perf_counter()
        before = footprint.resident_mb()
        gone = let_go()
        if self.frozen:
            self.release()
        else:
            gc.collect()
        footprint.hand_back()
        gc.freeze()
        self.frozen, self._settled = True, False
        python, malloc, free, arrow = footprint.allocated()
        figures: dict[str, Any] = {
            "after": "the review" if reviewed else "the session",
            "let_go": {name: n for name, n in gone.items() if n},
            "kept": {name: n for name, n in held().items() if name not in gone},
            "rss_before_mb": before,
            "rss_mb": footprint.resident_mb(),
            "swapped_mb": footprint.swapped_mb(),
            "python_blocks": python,
            "malloc_mb": malloc,
            "malloc_free_mb": free,
            "arrow_mb": arrow,
            "seconds": round(time.perf_counter() - started, 2),
        }
        log.info(
            "memory handed back after %s: let go of %s; kept %s; engine rss %s -> %s MB, swapped %s MB;"
            " python %d blocks, malloc %s MB in use and %s MB free, arrow %s MB; %.2fs",
            figures["after"],
            _counts(figures["let_go"]) or "nothing",
            _counts(figures["kept"]) or "nothing",
            _mb(before),
            _mb(figures["rss_mb"]),
            _mb(figures["swapped_mb"]),
            python,
            _mb(malloc),
            _mb(free),
            _mb(arrow),
            figures["seconds"],
        )
        return figures

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


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {n}" for name, n in counts.items())


def _mb(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"
