"""The statuses of a kind's algorithms (#179), judged within their kind, through the Replay
Harness (as ``tests/test_lifecycle.py`` for the direction's).

Input: a scored history of the test kind (``drift``) put in the store, one sure call per
bar close, each bar close's answer, and its baselines' calls; status rows as of earlier days;
then a replayed day, whose review runs at 15:40. Output: each algorithm's status in the
Engine API, judged against the drift's baselines, a clone looked for among the drift's
algorithms only.

The first eight bar closes from 09:01 each day were answered rose, held, fell, held, twice.
Always held (``drift-held``) is right on four, the last window's answer (``drift-persist``,
put here as always rose) on two.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds import KST
from krx_quant.kinds import Answer, KindForecast
from krx_quant.lifecycle import target_changed
from krx_quant.predictions import NewKindPrediction
from krx_quant.pricing import Direction
from tests.algorithm_doubles import FixedAlgorithm
from tests.kind_doubles import DRIFT, TEST_KIND, FixedKindAlgorithm
from tests.replay_events import DAY, SYMBOL, api, at, get_json, run
from tests.test_lifecycle import last_change, put_days, put_status, stored, weekdays_before

ANSWERS = ["rose", "held", "fell", "held"] * 2
BASELINE_CALLS = {"drift-held": ["held"] * len(ANSWERS), "drift-persist": ["rose"] * len(ANSWERS)}
RIGHT = ANSWERS
HELD = ["held"] * len(ANSWERS)  # always-held's calls: no lift
SEER = FixedKindAlgorithm("seer", features=("velocity",), unique=("velocity",))
COPY = FixedKindAlgorithm("copy", features=("acceleration",), unique=("acceleration",))
STILL = FixedKindAlgorithm("still", features=("relative_volume",), unique=("relative_volume",))
# A direction algorithm whose p_up − p_down moves as the seer's p_fell − p_rose.
ALPHA = FixedAlgorithm("alpha", features=("vwap_z",), unique=("vwap_z",))
AS_DIRECTION = {"fell": Direction.UP, "held": Direction.FLAT, "rose": Direction.DOWN}


def sure(label: str) -> KindForecast:
    first, middle, third = (1.0 if c == label else 0.0 for c in TEST_KIND.classes)
    return KindForecast((first, middle, third), {"drift_ticks": 0.0})


def put_kind_days(
    db: Path,
    days: list[date],
    calls: dict[str, list[str]],
    model: str = "v1",
    target_version: int | None = TEST_KIND.answer_version,
) -> None:
    """Each drift algorithm's ``calls`` on each of ``days`` (by ``model``, trained on
    ``target_version``: the drift's answers unless said), the baselines' calls, and each bar
    close's answer."""
    with stored(db) as store:
        store.record_models([(a, model, target_version) for a in calls])
        for d in days:
            first = datetime.combine(d, time(9, 1), KST)
            made = [
                NewKindPrediction(
                    SYMBOL,
                    algorithm,
                    "v1" if algorithm in BASELINE_CALLS else model,
                    first + timedelta(minutes=i),
                    first + timedelta(minutes=i),
                    TEST_KIND.output(sure(label)),
                )
                for algorithm, labels in {**calls, **BASELINE_CALLS}.items()
                for i, label in enumerate(labels)
            ]
            store.add_kind_predictions(DRIFT, made)
            store.add_kind_answers(
                DRIFT,
                TEST_KIND.answer_version,
                [(SYMBOL, first + timedelta(minutes=i), Answer(a), False) for i, a in enumerate(ANSWERS)],
            )


async def review_day(db: Path, kind_algorithms: list[FixedKindAlgorithm], **options: Any) -> Any:
    """Replay DAY past its 15:40 review, with no market events."""
    return await run(
        db,
        [],
        until=at("15:45:00"),
        kinds=(TEST_KIND,),
        kind_algorithms=kind_algorithms,
        min_sample=20,
        **options,
    )


async def listed(engine: Any) -> dict[str, Any]:
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    return {a["name"]: a for a in body["algorithms"]}


async def test_a_kind_s_algorithm_ends_probation_better_than_every_baseline_of_its_kind(
    tmp_path: Path,
) -> None:
    """Input: nine days of history, then the replayed day: ten on probation. The seer is right
    on every bar close (0.5 a day over always-held, 0.75 over persistence, every day: its lower
    bound is 0.5); still calls held, as always-held does. Output: the seer is active, judged
    on the drift's baselines; still stays on probation; the drift's leaderboard shows both."""
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)
    for name in ("seer", "still"):
        put_status(db, name, "probation", history[0])
    put_kind_days(db, history, {"seer": RIGHT, "still": HELD})

    engine = await review_day(db, [SEER, STILL])

    statuses = await listed(engine)
    assert last_change(statuses["seer"])[:2] == ("probation", "active")
    assert last_change(statuses["seer"])[2] == (
        "10 days on probation; weakest lift low 0.5000 by trading day at 98.3% (9 days scored)"
    )
    assert statuses["still"]["status"]["status"] == "probation"
    assert statuses["still"]["stage"]["weakest"]["baseline"] == "drift-held"
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?kind=drift&period=20d")
    assert {e["algorithm"]: e["status"] for e in board["ranked"] + board["unranked"]} == {
        "seer": "active",
        "still": "probation",
    }
    seer = next(e for e in board["unranked"] if e["algorithm"] == "seer")  # fewer than 200 scored
    assert set(seer["lift"]["vs"]) == {"drift-held", "drift-persist"}


async def test_a_clone_is_looked_for_within_its_kind_only(tmp_path: Path) -> None:
    """Input: five days of the seer, a copy of it registered after it, and a direction
    algorithm whose p_up − p_down moves exactly as the seer's p_fell − p_rose, all on probation.
    Output: the copy is a clone of the seer; the seer is no clone of the direction's algorithm,
    nor the direction's of it."""
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 5)
    for name in ("alpha", "seer", "copy"):
        put_status(db, name, "probation", days[0])
    put_kind_days(db, days, {"seer": RIGHT, "copy": RIGHT})
    put_days(db, days, {"alpha": [AS_DIRECTION[a] for a in RIGHT]})

    engine = await review_day(db, [SEER, COPY], algorithms=[ALPHA])

    statuses = await listed(engine)
    assert last_change(statuses["copy"])[:2] == ("probation", "clone")
    assert last_change(statuses["copy"])[2] == "p_fell - p_rose moves with seer (r = 1.000)"
    assert statuses["seer"]["status"]["status"] == "probation"
    assert statuses["alpha"]["status"]["status"] == "probation"


async def test_a_kind_s_defaults_are_not_counted_toward_its_probation(tmp_path: Path) -> None:
    """Input: the seer's history made by its defaults (no target recorded). Output: nothing is
    counted: its stage waits for a model trained on the drift's answers."""
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)
    put_status(db, "seer", "probation", history[0])
    put_kind_days(db, history, {"seer": RIGHT}, model="seer-default", target_version=None)

    engine = await review_day(db, [SEER])

    seer = (await listed(engine))["seer"]
    assert seer["status"]["status"] == "probation"
    assert seer["stage"]["counted_from"] is None


# A kind's answer version going up (#187: the barrier's, to 2, crossed quotes counted): its
# algorithms restart on probation once, as the direction's did when its target changed (#170).

DRIFT_V2 = replace(TEST_KIND, answer_version=2)
CALM = replace(TEST_KIND, name="calm", baselines=())  # another kind, its answer version still 1


class Calm(FixedKindAlgorithm):
    kind = "calm"


async def test_a_kind_s_algorithms_restart_once_when_its_answer_version_goes_up(tmp_path: Path) -> None:
    """Input: alpha (the direction), the seer (the drift) and still (another kind, calm), all
    active; the direction's target already marked; the drift's answer goes to version 2, the
    calm one's stays at 1. Output: the seer is back on probation, the reason naming the drift's
    new version, once (an engine started again leaves it be); alpha and still stay active; a
    ``target`` run marks the drift's version 2."""
    db = tmp_path / "engine.db"
    since = weekdays_before(DAY, 30)[0]
    for name in ("alpha", "seer", "still"):
        put_status(db, name, "active", since)
    still = Calm("still", features=("relative_volume",), unique=("relative_volume",))
    options: dict[str, Any] = {
        "algorithms": [ALPHA],
        "kinds": (DRIFT_V2, CALM),
        "kind_algorithms": [SEER, still],
    }

    await run(db, [], start=at("06:00:00"), until=at("06:01:00"), **options)
    again = await run(db, [], start=at("06:30:00"), until=at("06:31:00"), **options)

    statuses = await listed(again)
    assert last_change(statuses["seer"]) == ("active", "probation", target_changed(DRIFT, 2))
    assert statuses["seer"]["status"]["since"] == "2026-10-12T06:00:00+09:00", "once, at the deploy"
    assert (statuses["alpha"]["status"]["status"], statuses["still"]["status"]["status"]) == (
        "active",
        "active",
    )
    with stored(db) as store:
        assert store.target_marked(2, DRIFT) and not store.target_marked(1, "calm")
