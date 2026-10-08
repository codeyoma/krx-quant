"""Algorithms on the prediction path, through the Replay Harness.

Input: hand-made events, a fake clock, and registered algorithms (Newton or stand-ins).
Output: Engine API responses and what is stored in SQLite.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model
from krx_quant.baselines import Forecast
from krx_quant.engine import Budget
from krx_quant.features import FEATURES, FeatureStore, Snapshot
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import SYMBOL, api, at, get_json, run
from tests.test_replay import busy_morning


async def predictions_of(engine: Any, algorithm: str, limit: int = 1000) -> list[dict[str, Any]]:
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm={algorithm}&limit={limit}")
    predictions: list[dict[str, Any]] = body["predictions"]
    return predictions


async def test_newton_predicts_and_is_scored_under_its_model_version(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("10:00:00"))

    newton = await predictions_of(engine, "newton")
    assert len(newton) == 60  # 09:01–10:00
    assert {p["model_version"] for p in newton} == {"newton-default"}
    scored = [p for p in newton if p["outcome"] is not None]
    assert len(scored) == 56  # 09:57–10:00 wait for their targets
    assert all(p["outcome"]["class_a"] for p in scored[:-20])
    # Once there are enough bars to see a trend, Newton commits to a move.
    assert any(p["expected_ticks"] != 0 for p in newton)


async def test_a_trained_model_is_used_and_recorded(tmp_path: Path) -> None:
    trained = Model("newton-20261009-abc123", {"damping": 0.0, "threshold": 0.0})

    engine = await run(
        tmp_path / "engine.db", busy_morning(), until=at("09:40:00"), models={"newton": trained}
    )

    assert {p["model_version"] for p in await predictions_of(engine, "newton")} == {"newton-20261009-abc123"}
    async with api(engine) as client:
        [newton] = (await get_json(client, "/v1/algorithms"))["algorithms"]
    assert newton["name"] == "newton"
    assert newton["card"]["scientist"] == "Isaac Newton"
    assert set(newton["card"]["unique_features"]) <= set(newton["features"])
    assert newton["model"] == {
        "version": "newton-20261009-abc123",
        "params": dict(trained.params),
        "trained": None,
        "trained_until": None,
    }


async def test_each_feature_is_computed_once_per_symbol_and_bar_close_for_everyone(tmp_path: Path) -> None:
    computed: Counter[str] = Counter()

    def counted(name: str) -> Any:
        def feature(s: Snapshot) -> float | None:
            computed[name] += 1
            return FEATURES[name](s)

        return feature

    store = FeatureStore({name: counted(name) for name in FEATURES})
    first = FixedAlgorithm("first", features=("velocity", "acceleration"), unique=("acceleration",))
    second = FixedAlgorithm("second", features=("velocity", "relative_volume"), unique=("relative_volume",))

    await run(
        tmp_path / "engine.db",
        busy_morning(),
        until=at("09:30:00"),
        algorithms=[first, second],
        feature_store=store,
    )

    bar_closes = 30  # 09:01–09:30, one symbol
    assert computed == {"velocity": bar_closes, "acceleration": bar_closes, "relative_volume": bar_closes}
    # Both saw the same velocity at every bar close.
    assert [s["velocity"] for s in first.seen] == [s["velocity"] for s in second.seen]
    assert any(s["velocity"] is not None for s in first.seen)


async def test_an_algorithm_over_its_time_budget_is_skipped_and_recorded(tmp_path: Path) -> None:
    quick = FixedAlgorithm("quick", features=("velocity",), unique=("velocity",))
    slow = FixedAlgorithm("slow", features=("acceleration",), unique=("acceleration",), seconds=0.5)

    engine = await run(
        tmp_path / "engine.db",
        busy_morning(),
        until=at("09:01:30"),
        algorithms=[quick, slow],
        budget=Budget(per_algorithm=0.1),
    )

    [skipped] = await predictions_of(engine, "slow")
    assert (skipped["status"], skipped["p_up"], skipped["outcome"]) == ("timed_out", None, None)
    assert skipped["model_version"] == "slow-default"
    [made] = await predictions_of(engine, "quick")
    assert (made["status"], made["p_flat"]) == ("ok", 0.6)


async def test_when_the_cpu_budget_runs_short_the_last_registered_is_skipped_first(tmp_path: Path) -> None:
    first = FixedAlgorithm("first", features=("velocity",), unique=("velocity",), seconds=0.01)
    second = FixedAlgorithm("second", features=("acceleration",), unique=("acceleration",), seconds=0.01)
    third = FixedAlgorithm("third", features=("relative_volume",), unique=("relative_volume",), seconds=0.3)

    engine = await run(
        tmp_path / "engine.db",
        busy_morning(),
        until=at("09:03:30"),
        algorithms=[first, second, third],
        budget=Budget(cpu=0.2),
    )

    # 09:01 measures each one's time; at 09:02 the three together are over 0.2 seconds.
    assert len(await predictions_of(engine, "first")) == 3
    assert len(await predictions_of(engine, "second")) == 3
    # Skipping halves its expected time, so at 09:03 it is tried (and timed) again.
    third_closes = sorted(p["bar_close"][11:16] for p in await predictions_of(engine, "third"))
    assert third_closes == ["09:01", "09:03"]
    with sqlite3.connect(tmp_path / "engine.db") as conn:
        runs = conn.execute("SELECT kind, at, algorithm FROM runs").fetchall()
    assert runs == [("cpu_budget", "2026-10-12T09:02:00+09:00", "third")]


class Raising(FixedAlgorithm):
    def predict(self, features: Any, model: Any) -> Any:
        raise TimeoutError("its own timeout, not the engine's")


async def test_an_algorithm_that_fails_or_breaks_the_contract_is_recorded_as_an_error(tmp_path: Path) -> None:
    raising = Raising("raising", features=("velocity",), unique=("velocity",))
    endless = FixedAlgorithm(
        "endless",
        features=("acceleration",),
        unique=("acceleration",),
        forecast=Forecast(0.2, 0.6, 0.2, float("inf")),
    )

    engine = await run(
        tmp_path / "engine.db", busy_morning(), until=at("09:01:30"), algorithms=[raising, endless]
    )

    for name in ("raising", "endless"):
        [failed] = await predictions_of(engine, name)
        assert (failed["status"], failed["p_up"], failed["expected_ticks"]) == ("error", None, None)
