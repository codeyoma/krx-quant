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
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import DAY, api, at, get_json, run
from tests.test_leaderboard_api import OUTCOMES as FORTY_OUTCOMES
from tests.test_leaderboard_api import SURE

ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
GAMMA = FixedAlgorithm("gamma", features=("relative_volume",), unique=("relative_volume",))
DELTA = FixedAlgorithm("delta", features=("vwap_z",), unique=("vwap_z",))
NEXT_DAY = date(2026, 10, 13)
TWO_DAYS = MarketCalendar({DAY: True, NEXT_DAY: True})
# What happened at each day's first eight bar closes from 09:01: up, flat, down, flat, twice.
# Enough for min_sample 20 within three days, and few enough to fill 65 days fast.
OUTCOMES = FORTY_OUTCOMES[:8]
BASELINE_CALLS = {
    "baseline-flat": [Direction.FLAT] * len(OUTCOMES),
    "baseline-persist": [Direction.UP] * len(OUTCOMES),
    "baseline-random": [Direction.DOWN] * len(OUTCOMES),
}
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
    """Each algorithm's ``calls`` on each of ``days`` (one sure call per bar close from 09:01,
    scored against OUTCOMES on both bases), with the baselines' unless they are there.
    """
    if with_baselines:
        calls = {**calls, **BASELINE_CALLS}
    with stored(db) as store:
        for d in days:
            made = []
            for algorithm, directions in calls.items():
                for i, call in enumerate(directions):
                    t = datetime.combine(d, time(9, 1), KST) + timedelta(minutes=i)
                    target = t + timedelta(minutes=4)
                    made.append(
                        NewPrediction("005930", algorithm, "v1", t, t, SURE[call], target, 81_250, 81_250, 2)
                    )
            pending = store.add_all(made)
            scores = []
            for p in pending:
                outcome = OUTCOMES[
                    (p.target_at - datetime.combine(d, time(9, 5), KST)) // timedelta(minutes=1)
                ]
                target = 81_250 + {Direction.UP: 300, Direction.FLAT: 0, Direction.DOWN: -300}[outcome]
                scores.append((p.prediction_id, Score(target, outcome, outcome, None)))
            store.score_all(scores, datetime.combine(d, time(15, 30), KST))


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


# Wrong where it matters: down on the ups, up on the downs, flat on the flats. It beats no
# baseline.
WRONG = [{Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP}.get(o, o) for o in OUTCOMES]


async def test_an_active_algorithm_no_better_than_a_baseline_is_benched_and_still_predicts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 25)
    put_status(db, "alpha", "active", days[0])
    put_status(db, "beta", "active", days[0])
    # Good for its first days, then wrong for the last twenty.
    put_days(db, days[:5], {"alpha": RIGHT, "beta": MISSES_DOWNS})
    put_days(db, days[5:], {"alpha": WRONG, "beta": MISSES_DOWNS})

    engine = await review_day(db, [ALPHA, BETA])

    statuses = await listed(engine)
    assert last_change(statuses["alpha"])[:2] == ("active", "bench")
    assert "last 20 days" in last_change(statuses["alpha"])[2]
    assert statuses["beta"]["status"]["status"] == "active"
    async with api(engine) as client:
        body = await get_json(client, "/v1/symbols/005930/predictions?algorithm=alpha&limit=5")
    assert len(body["predictions"]) == 5  # a benched algorithm is still run


async def test_a_benched_algorithm_recovers_or_after_sixty_days_is_a_retirement_candidate(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 65)
    put_status(db, "alpha", "bench", days[0])
    put_status(db, "beta", "bench", days[0])
    put_days(db, days[:-20], {"alpha": WRONG, "beta": WRONG})
    put_days(db, days[-20:], {"alpha": WRONG, "beta": RIGHT})  # beta is good again
    sent: list[str] = []

    engine = await review_day(db, [ALPHA, BETA], notify=sent.append)

    statuses = await listed(engine)
    assert last_change(statuses["beta"])[:2] == ("bench", "active")
    assert statuses["alpha"]["status"]["status"] == "bench"  # retired only by command
    [alert] = sent
    assert "alpha" in alert and "krx-quant retire alpha" in alert

    # The next day it is still a candidate, but the alert is not sent again.
    await run(
        db,
        [],
        until=at("15:45:00", NEXT_DAY),
        calendar=TWO_DAYS,
        algorithms=[ALPHA, BETA],
        min_sample=20,
        notify=sent.append,
    )
    assert len(sent) == 1


async def test_an_algorithm_on_probation_moving_with_an_earlier_one_is_a_clone(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 5)
    for name in ("alpha", "beta", "gamma"):
        put_status(db, name, "probation", days[0])
    put_status(db, "delta", "clone", days[0])
    # beta says what alpha says; gamma does not; delta stopped copying anyone.
    put_days(db, days, {"alpha": RIGHT, "beta": RIGHT, "gamma": MISSES_DOWNS, "delta": WRONG})

    engine = await review_day(db, [ALPHA, BETA, GAMMA, DELTA])

    statuses = await listed(engine)
    assert last_change(statuses["beta"])[:2] == ("probation", "clone")
    assert "alpha" in last_change(statuses["beta"])[2]
    assert statuses["alpha"]["status"]["status"] == "probation"  # the earlier one is not the clone
    assert statuses["gamma"]["status"]["status"] == "probation"
    assert last_change(statuses["delta"])[:2] == ("clone", "probation")
