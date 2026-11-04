"""Scored days for the lifecycle's tests (#511): the weekdays before a day, each with its first eight
bar closes called and scored against made-up outcomes, the baselines' with them, a prediction store
open for a block, and an outbox for the alerts sent.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.feeds import KST
from krx_quant.predictions import NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.touch import TARGET_VERSION
from tests.api_doubles import OUTCOMES as FORTY_OUTCOMES
from tests.api_doubles import SURE, touch

# What happened at each day's first eight bar closes from 09:01: up, flat, down, flat, twice.
# Enough for min_sample 20 within three days, and few enough to fill 65 days fast.
OUTCOMES = FORTY_OUTCOMES[:8]
BASELINE_CALLS = {
    "baseline-flat": [Direction.FLAT] * len(OUTCOMES),
    "baseline-persist": [Direction.UP] * len(OUTCOMES),
    "baseline-random": [Direction.DOWN] * len(OUTCOMES),
}
RIGHT = OUTCOMES  # every call right


def weekdays_before(day: date, n: int) -> list[date]:
    """The ``n`` weekdays before ``day``, oldest first."""
    days: list[date] = []
    d = day
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days[::-1]


@contextlib.contextmanager
def stored(db: Path) -> Iterator[PredictionStore]:
    store = PredictionStore(db)
    try:
        yield store
    finally:
        store.close()


def put_days(
    db: Path,
    days: list[date],
    calls: dict[str, list[Direction]],
    *,
    with_baselines: bool = True,
    model: str = "v1",
    target_version: int | None = TARGET_VERSION,
) -> None:
    """Each algorithm's ``calls`` on each of ``days`` (one sure call per bar close from 09:01,
    by ``model``, trained on ``target_version``: the touch target unless said), scored against
    OUTCOMES: the bar closes' touch labels (and, unread, the old bases' classes), with the
    baselines' unless they are there.
    """
    with stored(db) as store:
        store.record_models([(a, model, target_version) for a in calls])
        if with_baselines:
            calls = {**calls, **BASELINE_CALLS}
        for d in days:
            made = []
            for algorithm, directions in calls.items():
                version = "v1" if algorithm in BASELINE_CALLS else model
                for i, call in enumerate(directions):
                    t = datetime.combine(d, time(9, 1), KST) + timedelta(minutes=i)
                    at_target = t + timedelta(minutes=4)
                    made.append(
                        NewPrediction(
                            "005930", algorithm, version, t, t, SURE[call], at_target, 81_250, 81_250, 2
                        )
                    )
            pending = store.add_all(made).pending
            scores = []
            for p in pending:
                outcome = OUTCOMES[
                    (p.target_at - datetime.combine(d, time(9, 5), KST)) // timedelta(minutes=1)
                ]
                mid = 81_250 + {Direction.UP: 300, Direction.FLAT: 0, Direction.DOWN: -300}[outcome]
                scores.append((p.prediction_id, Score(mid, outcome, outcome, None)))
            store.score_all(scores, datetime.combine(d, time(15, 30), KST))
            first = datetime.combine(d, time(9, 1), KST)
            store.add_touches(
                [touch("005930", first + timedelta(minutes=i), o) for i, o in enumerate(OUTCOMES)]
            )


class Outbox:
    """Alerts sent, as Telegram would take them, or refuse them while ``down``."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.down = False

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return not self.down
