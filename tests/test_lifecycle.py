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

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds import KST
from krx_quant.predictions import PredictionStore
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import api, at, get_json, run

ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))


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


def put_status(db: Path, algorithm: str, status: str, since: date) -> None:
    store = PredictionStore(db)
    store.add_status(algorithm, morning(since), None, status, "set by the test")
    store.close()
