"""Each algorithm's day by day in the Engine API (#561, ``algorithm_days.py``): ``/v1/leaderboard/days``.

Input: predictions with made-up probabilities stored through the prediction store on two days, with
their bar closes' touch labels, the statuses they had, a simulated probation's replay and a rescore
run. Output: each day's hit rate, coverage, precision by class, lift over the climate baseline and log
loss as computed by hand; the status of each day, an active algorithm's decay mark, the consensus
beside its members; and the days computed once, not again at each request.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithm_days import Versus, decay_json
from krx_quant.api import create_app
from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.baselines import Forecast
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KST
from krx_quant.lifecycle import Rules, Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.stats import entry, on_target, scored_by_algorithm, split
from krx_quant.touch import TARGET_VERSION
from tests.api_doubles import barrier_board, get, touch
from tests.api_schema import check
from tests.lifecycle_doubles import stored
from tests.replay_events import DAY, at, run

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
EARLIER = date(2026, 10, 8)  # the Thursday before DAY (Friday the 9th is Hangul Day)
# What happened at each day's four bar closes from 09:01.
HAPPENED = [UP, FLAT, DOWN, FLAT]
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
CLIMATE_CALLS = [SURE[FLAT]] * 4  # the climate baseline's: always flat, right at the two flats
# Alpha's on DAY: up (right), flat (right), up (wrong: down happened), flat (right).
ALPHA = [
    Forecast(0.6, 0.3, 0.1, 0),
    Forecast(0.2, 0.5, 0.3, 0),
    Forecast(0.5, 0.2, 0.3, 0),
    Forecast(0.1, 0.6, 0.3, 0),
]


def morning(day: date) -> datetime:
    return datetime.combine(day, time(8), KST)


def store_calls(
    store: PredictionStore, algorithm: str, forecasts: Sequence[Forecast], day: date, *, version: str = "v1"
) -> None:
    """``forecasts`` at ``day``'s bar closes from 09:01, scored on HAPPENED's touch labels; an
    algorithm's by a model trained on the touch target (#170)."""
    if not algorithm.startswith(("baseline-", "consensus-")):
        store.record_models([(algorithm, version, TARGET_VERSION)])
    start = datetime.combine(day, time(9, 1), KST)
    for i, forecast in enumerate(forecasts):
        t = start + timedelta(minutes=i)
        [pending] = store.add_all(
            [
                NewPrediction(
                    "005930", algorithm, version, t, t, forecast, t + timedelta(minutes=4), 81_250, 81_250, 2
                )
            ]
        ).pending
        label = HAPPENED[i]
        mid = 81_250 + {UP: 300, FLAT: 0, DOWN: -300}[label]
        store.score_all([(pending.prediction_id, Score(mid, label, label, None))], t + timedelta(minutes=4))
        store.add_touches([touch("005930", t, label)])


Put = Callable[[PredictionStore], None]


@contextlib.asynccontextmanager
async def series(
    tmp_path: Path, put: Put, *, min_sample: int = 4
) -> AsyncIterator[tuple[PredictionEngine, TestClient[Any, Any]]]:
    """An engine on a holiday (DAY), its store first given ``put``'s: EARLIER and DAY are both
    reviewed (the next review is Tuesday's)."""
    db = tmp_path / "engine.db"
    with stored(db) as store:
        put(store)
    engine = await run(
        db,
        [],
        until=at("16:00:00"),
        calendar=MarketCalendar({DAY: False}),
        target_version=TARGET_VERSION,
        algorithms=[],
        kind_algorithms=(),
        min_sample=min_sample,
    )
    async with TestClient(TestServer(create_app(engine))) as client:
        yield engine, client


def two_days(store: PredictionStore) -> None:
    """Alpha on probation from EARLIER, active from DAY (the review after EARLIER's close made it
    active): wrong at every bar close of EARLIER, ALPHA on DAY; the climate baseline both days; the
    final consensus on DAY, right at the first and the third only."""
    store.add_status("alpha", morning(EARLIER), None, Status.PROBATION, "registered")
    store.add_status(
        "alpha", datetime.combine(EARLIER, time(15, 40), KST), Status.PROBATION, Status.ACTIVE, "test"
    )
    store.mark_target(TARGET_VERSION, morning(EARLIER))
    store_calls(store, "alpha", [SURE[DOWN], SURE[UP], SURE[UP], SURE[DOWN]], EARLIER)
    store_calls(store, "alpha", ALPHA, DAY)
    for day in (EARLIER, DAY):
        store_calls(store, "baseline-climate", CLIMATE_CALLS, day)
    store_calls(store, "consensus-final", [SURE[UP], SURE[DOWN], SURE[DOWN], SURE[UP]], DAY)


def of(body: dict[str, Any], group: str, name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(s for s in body[group] if s["algorithm"] == name)
    return found


async def test_each_day_s_values_are_computed_from_its_stored_predictions_and_their_answers(
    tmp_path: Path,
) -> None:
    async with series(tmp_path, two_days) as (_, client):
        body = await get(client, "/v1/leaderboard/days")

    check("leaderboard_days", body)
    assert (body["kind"], body["days"], body["climate"]) == (
        "direction",
        ["2026-10-08", "2026-10-12"],
        "baseline-climate",
    )
    alpha = of(body, "algorithms", "alpha")
    earlier, day = alpha["days"]
    assert (earlier["day"], earlier["status"], earlier["simulated"]) == ("2026-10-08", "probation", False)
    assert (earlier["n"], earlier["accuracy"], earlier["coverage"]) == (4, 0.0, 1.0), (
        "four sure calls, all wrong"
    )
    assert earlier["lift"] == {"n": 4, "value": -0.5}, "the climate was right at the two flats"
    assert earlier["log_loss"] == pytest.approx(-math.log(1e-6)), "floored as the nightly training's"
    # DAY: up right, flat right, up wrong, flat right.
    assert (day["day"], day["status"]) == ("2026-10-12", "active")
    assert (day["n"], day["accuracy"], day["coverage"]) == (4, 0.75, 0.5)
    assert day["precision"] == {
        "up": {"n": 2, "value": 0.5},
        "flat": {"n": 2, "value": 1.0},
        "down": {"n": 0, "value": None},
    }
    assert day["lift"] == {"n": 4, "value": 0.25}, "right at the first, where the climate was not"
    assert day["log_loss"] == pytest.approx(
        -(math.log(0.6) + math.log(0.5) + math.log(0.3) + math.log(0.6)) / 4
    )
    climate = of(body, "baselines", "baseline-climate")
    assert climate["status"] is None and [d["accuracy"] for d in climate["days"]] == [0.5, 0.5]
    assert all(d["lift"] is None for d in climate["days"]), "itself"
    assert [s["algorithm"] for s in body["baselines"]] == sorted(
        ["baseline-flat", "baseline-persist", "baseline-random", "baseline-climate"]
    )
    assert of(body, "baselines", "baseline-flat")["days"] == [], "no scored prediction: no day"
    consensus = of(body, "consensus", "consensus-final")
    assert [d["day"] for d in consensus["days"]] == ["2026-10-12"]


async def test_the_consensus_is_set_beside_the_members_active_at_the_day_s_close(tmp_path: Path) -> None:
    async with series(tmp_path, two_days) as (_, client):
        body = await get(client, "/v1/leaderboard/days")

    [beside] = body["members"]  # on EARLIER, no consensus prediction
    assert beside["day"] == "2026-10-12" and beside["members"] == ["alpha"], "active from DAY"
    assert (beside["n"], beside["accuracy"]) == (4, 0.5)
    assert beside["vs"] == {"alpha": {"n": 4, "accuracy": 0.75, "consensus": 0.5, "lift": -0.25}}
    assert (beside["best"], beside["lift"]) == ("alpha", -0.25), "alpha alone did better"


async def test_an_active_algorithm_carries_its_decay_on_its_days_active(tmp_path: Path) -> None:
    def worse(store: PredictionStore) -> None:
        two_days(store)
        for day in (EARLIER, DAY):  # the other baselines: always flat too, as the climate
            for name in ("baseline-flat", "baseline-persist", "baseline-random"):
                store_calls(store, name, CLIMATE_CALLS, day)

    async with series(tmp_path, worse) as (engine, client):
        body = await get(client, "/v1/leaderboard/days")
        rows = on_target(engine.store.reviewed([DAY], False), engine.baseline_names, TARGET_VERSION)

    decay = of(body, "algorithms", "alpha")["decay"]
    # Its day active is DAY alone: EARLIER's close came before the review that made it active.
    assert decay == {
        "days": 1,
        "rule_days": 10,
        "weakest": {"baseline": "baseline-climate", "n": 4, "value": 0.25},
        "enough": True,
        "flagged": False,
    }
    # As the bench check takes the weakest lift on the same days (``lifecycle``, #545).
    algorithms, baselines = split(scored_by_algorithm(rows), engine.baseline_names)
    assert entry("alpha", algorithms["alpha"], baselines, engine.fee_rate).weakest_lift == 0.25
    assert of(body, "baselines", "baseline-climate")["decay"] is None


def test_a_decay_is_flagged_at_no_lift_or_less_with_enough_to_judge() -> None:
    from krx_quant.algorithm_days import Day, Tally

    def day(d: date, gain: int) -> Day:
        vs = {"baseline-flat": Versus(10, 5 + gain, 5), "baseline-climate": Versus(10, 6, 5)}
        return Day(d, {"alpha": Tally(10, 5, (0, 10, 0), (0, 5, 0), 7.0, vs)})

    days = [day(date(2026, 10, 13), -1), day(date(2026, 10, 12), 0)]  # latest first
    active = dict.fromkeys((d.day for d in days), "active")
    since = morning(date(2026, 10, 12))
    baselines = {"baseline-flat", "baseline-climate"}

    flagged = decay_json("alpha", since, days, active, baselines, Rules(), 20, 20)
    few = decay_json("alpha", since, days, active, baselines, Rules(), 21, 20)

    assert flagged["weakest"] == {"baseline": "baseline-flat", "n": 20, "value": -0.05}
    assert (flagged["enough"], flagged["flagged"]) == (True, True)
    assert (few["enough"], few["flagged"]) == (False, False), "20 pairs, fewer than 21: not judged"


async def test_a_simulated_day_shows_the_simulation_s_values_on_probation(tmp_path: Path) -> None:
    """#298: beta's probation (begun on DAY) was simulated on EARLIER; it made no live prediction then."""

    def simulated(store: PredictionStore) -> None:
        two_days(store)
        store.add_status("beta", morning(DAY), None, Status.PROBATION, "registered")
        store.record_models([("beta", "v1", TARGET_VERSION)])
        start = datetime.combine(EARLIER, time(9, 1), KST)
        climate = store.made_on([EARLIER])
        rows = [
            {
                "symbol": "005930",
                "algorithm": "beta",
                "model_version": "v1",
                "model_target": TARGET_VERSION,
                "bar_close": (start + timedelta(minutes=i)).isoformat(),
                "status": "ok",
                "p_up": float(o is UP),
                "p_flat": float(o is FLAT),
                "p_down": float(o is DOWN),
                "touch_class": o.value,
            }
            for i, o in enumerate(HAPPENED)  # right at each
        ] + [r for r in climate if r["algorithm"] == "baseline-climate"]
        run_id = store.add_simulation_run(morning(DAY), "bar_close", [EARLIER])
        store.add_sim_predictions(run_id, rows, TARGET_VERSION)
        store.add_simulation("beta", morning(DAY), "direction", run_id, morning(DAY), {})

    async with series(tmp_path, simulated) as (_, client):
        body = await get(client, "/v1/leaderboard/days")

    [replayed] = of(body, "algorithms", "beta")["days"]
    assert (replayed["day"], replayed["status"], replayed["simulated"]) == ("2026-10-08", "probation", True)
    assert (replayed["n"], replayed["accuracy"]) == (4, 1.0)
    assert replayed["lift"] == {"n": 4, "value": 0.5}, "over the replay's climate"
    assert of(body, "algorithms", "beta")["status"] == "probation"


async def test_a_day_is_computed_once_and_again_only_once_its_scores_may_have_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with series(tmp_path, two_days) as (engine, client):
        reviewed = engine.store.reviewed
        asked: list[list[date]] = []

        def counted(
            days: Sequence[date], include_special: bool, algorithms: Any = None
        ) -> list[dict[str, Any]]:
            asked.append(list(days))
            return reviewed(days, include_special, algorithms)

        monkeypatch.setattr(engine.store, "reviewed", counted)
        first = await get(client, "/v1/leaderboard/days")
        assert sorted(d for days in asked for d in days) == [EARLIER, DAY], "a day's rows at a time"
        asked.clear()

        again = await get(client, "/v1/leaderboard/days?limit=1")
        store_calls(engine.store, "beta", ALPHA[:1], DAY + timedelta(days=1))  # a new bar close: not reviewed
        later = await get(client, "/v1/leaderboard/days")
        assert asked == [], "kept: neither a request nor a new bar close computes them again"
        assert later == first and again["days"] == ["2026-10-12"]

        engine.store.add_run("rescore", at("17:00:00"), None, "[]")  # a command rewrote past scores
        await get(client, "/v1/leaderboard/days")
        assert sorted(d for days in asked for d in days) == [EARLIER, DAY], "computed again"


async def test_a_kind_s_days_are_over_its_climate_on_its_classes(tmp_path: Path) -> None:
    def barriers(store: PredictionStore) -> None:
        barrier_board(store)  # DAY: the climate always times out, alpha always right

    async with series(tmp_path, barriers) as (_, client):
        body = await get(client, f"/v1/leaderboard/days?kind={BARRIER.name}")

    check("leaderboard_days", body)
    assert (body["kind"], body["climate"], body["days"]) == ("barrier", CLIMATE, ["2026-10-12"])
    [day] = of(body, "algorithms", "alpha")["days"]
    assert (day["n"], day["accuracy"], day["coverage"]) == (40, 1.0, 0.5)
    assert set(day["precision"]) == {UP_FIRST, TIMEOUT, DOWN_FIRST}
    assert day["lift"] == {"n": 40, "value": 0.5}, "the climate times out: right at half"
    assert body["consensus"] == [] and body["members"] == [], "the direction's only"


@pytest.mark.parametrize(("query", "status"), [("?limit=0", 400), ("?limit=61", 400), ("?kind=nothing", 400)])
async def test_a_wrong_query_is_a_400(tmp_path: Path, query: str, status: int) -> None:
    async with series(tmp_path, two_days) as (_, client):
        await get(client, f"/v1/leaderboard/days{query}", status)
