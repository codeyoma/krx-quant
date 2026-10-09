"""Algorithm status (probation, active, bench, retired, clone) through the Replay Harness.

Input: scored history put in the prediction store (one sure call per bar close, as in the
leaderboard tests) and status rows as of earlier days; then a replayed day, whose daily
review runs at 15:40, after the close. Output: each algorithm's status and history in
the Engine API, the alerts sent, and what the engine predicted.

Every rule reads the statistics the leaderboard shows: basis B, special predictions left
out, the weakest lift over the three baselines, and only with enough scored predictions
and pairs (``min_sample``, 20 here).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds import KST
from krx_quant.predictions import PredictionStore
from krx_quant.pricing import Direction
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import DAY, api, at, get_json, run
from tests.test_leaderboard_api import OUTCOMES, baselines, store_scored

ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
GAMMA = FixedAlgorithm("gamma", features=("relative_volume",), unique=("relative_volume",))
RIGHT = OUTCOMES  # every call right
FLAT = [Direction.FLAT] * len(OUTCOMES)  # the same calls as always-flat: no lift
# Right but on the downs, which it calls flat: better than every baseline, and its p_up − p_down
# moves with RIGHT's at a correlation of 0.82 (0.25 / √(0.5 × 0.1875)), not a clone.
MISSES_DOWNS = [Direction.FLAT if o is Direction.DOWN else o for o in OUTCOMES]


def weekdays_before(day: date, n: int) -> list[date]:
    """The ``n`` weekdays before ``day``, oldest first."""
    days: list[date] = []
    d = day
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days[::-1]


def morning(day: date) -> datetime:
    return datetime.combine(day, time(8), KST)


async def review_day(db: Path, algorithms: list[FixedAlgorithm], **options: Any) -> Any:
    """Replay DAY past its 15:40 review, with no market events."""
    return await run(db, [], until=at("15:45:00"), algorithms=algorithms, min_sample=20, **options)


async def listed(engine: Any) -> dict[str, Any]:
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    return {a["name"]: a for a in body["algorithms"]}


async def test_a_new_algorithm_starts_on_probation(tmp_path: Path) -> None:
    engine = await review_day(tmp_path / "engine.db", [ALPHA])

    alpha = (await listed(engine))["alpha"]
    assert alpha["status"]["status"] == "probation"
    [entered] = alpha["status"]["history"]
    assert (entered["from"], entered["to"], entered["reason"]) == (None, "probation", "registered")
    assert entered["at"] == "2026-10-12T00:00:00+09:00"  # when the engine first ran it


@contextlib.contextmanager
def stored(db: Path) -> Iterator[PredictionStore]:
    store = PredictionStore(db)
    try:
        yield store
    finally:
        store.close()


def put_status(db: Path, algorithm: str, status: str, since: date) -> None:
    with stored(db) as store:
        store.add_status(algorithm, morning(since), None, status, "set by the test")


def put_days(
    db: Path, days: list[date], calls: dict[str, list[Direction]], *, with_baselines: bool = True
) -> None:
    """Each algorithm's ``calls`` on each of ``days``, with the baselines' unless they are there."""
    with stored(db) as store:
        for d in days:
            if with_baselines:
                baselines(store, day=d)
            for algorithm, made in calls.items():
                store_scored(store, algorithm, made, day=d)


def last_change(algorithm: dict[str, Any]) -> tuple[str | None, str, str]:
    change = algorithm["status"]["history"][-1]
    return change["from"], change["to"], change["reason"]


async def test_probation_ends_after_ten_days_better_than_every_baseline(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)  # with the replayed day, ten days on probation
    for name in ("alpha", "gamma"):
        put_status(db, name, "probation", history[0])
    put_status(db, "beta", "probation", history[-3])  # four days so far
    put_days(db, history, {"alpha": RIGHT, "gamma": FLAT})
    put_days(db, history[-3:], {"beta": MISSES_DOWNS}, with_baselines=False)

    engine = await review_day(db, [ALPHA, BETA, GAMMA])

    statuses = await listed(engine)
    assert last_change(statuses["alpha"])[:2] == ("probation", "active")
    assert "lift" in last_change(statuses["alpha"])[2]
    assert statuses["alpha"]["status"]["since"] == "2026-10-12T15:40:00+09:00"
    assert statuses["beta"]["status"]["status"] == "probation"  # too soon
    assert statuses["gamma"]["status"]["status"] == "probation"  # no better than always-flat
