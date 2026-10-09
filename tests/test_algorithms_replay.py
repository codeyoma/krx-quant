"""Algorithms on the prediction path, through the Replay Harness.

Input: hand-made events, a fake clock, and registered algorithms (Newton or stand-ins).
Output: Engine API responses and what is stored in SQLite.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model
from krx_quant.baselines import Forecast
from krx_quant.engine import Budget
from krx_quant.features import FEATURES, FeatureStore, Snapshot
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import SYMBOL, api, at, get_json, run, trade
from tests.test_replay import busy_morning

VWAP_FEATURES = ("vwap_gap", "vwap_z", "reversion_speed")


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


async def test_gauss_predicts_and_is_scored_under_its_model_version(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("10:00:00"))

    gauss = await predictions_of(engine, "gauss")
    assert len(gauss) == 60  # 09:01–10:00
    assert {(p["model_version"], p["status"]) for p in gauss} == {("gauss-default", "ok")}
    assert len([p for p in gauss if p["outcome"] is not None]) == 56
    # Once the VWAP has a few bars behind it, Gauss expects the price to be pulled back.
    assert any(p["expected_ticks"] != 0 for p in gauss)


async def test_pre_market_trades_stay_out_of_the_vwap(tmp_path: Path) -> None:
    # The NXT pre-market trades on the integrated feed; the day's VWAP starts at the open.
    pre_market = [trade(at("08:30:00"), 90_000, 50_000), trade(at("08:45:00"), 70_000, 50_000)]

    def probe() -> FixedAlgorithm:
        return FixedAlgorithm("probe", features=VWAP_FEATURES, unique=VWAP_FEATURES)

    with_pre, without = probe(), probe()
    engine = await run(
        tmp_path / "pre.db", [*pre_market, *busy_morning()], until=at("09:40:00"), algorithms=[with_pre]
    )
    await run(tmp_path / "open.db", busy_morning(), until=at("09:40:00"), algorithms=[without])

    assert with_pre.seen == without.seen
    assert any(None not in s.values() for s in with_pre.seen)
    assert engine.bars(SYMBOL)[0].start == at("08:30:00")  # the trades did make bars


async def test_a_trained_model_is_used_and_recorded(tmp_path: Path) -> None:
    trained = Model("newton-20261009-abc123", {"damping": 0.0, "threshold": 0.0})

    engine = await run(
        tmp_path / "engine.db", busy_morning(), until=at("09:40:00"), models={"newton": trained}
    )

    assert {p["model_version"] for p in await predictions_of(engine, "newton")} == {"newton-20261009-abc123"}
    async with api(engine) as client:
        newton, gauss = (await get_json(client, "/v1/algorithms"))["algorithms"]
    assert (newton["name"], gauss["name"]) == ("newton", "gauss")
    assert gauss["model"]["version"] == "gauss-default"
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


async def test_a_trade_received_after_a_bar_close_stays_out_of_its_features(tmp_path: Path) -> None:
    # Live, the engine may run a bar close late, after later trades have arrived. A trade
    # traded in the closing minute but received after 09:31 must not reach 09:31's features.
    late = trade(at("09:31:00.5"), 81_200, 50_000, traded=at("09:30:59"))
    events = [*busy_morning(), late]
    delayed = timedelta(seconds=2)  # the 09:31 bar close runs at 09:31:02, after the late trade

    with_late = await run(tmp_path / "late.db", events, until=at("09:32:30"), display_delay=delayed)
    without = await run(tmp_path / "on-time.db", busy_morning(), until=at("09:32:30"), display_delay=delayed)

    def at_0931(predictions: list[dict[str, Any]]) -> dict[str, Any]:
        return next(p for p in predictions if p["bar_close"] == "2026-10-12T09:31:00+09:00")

    late_newton = at_0931(await predictions_of(with_late, "newton"))
    on_time_newton = at_0931(await predictions_of(without, "newton"))
    for field in ("p_up", "p_flat", "p_down", "expected_ticks"):
        assert late_newton[field] == on_time_newton[field], field
    # It does count from the next bar close on.
    assert at_0931_next(await predictions_of(with_late, "newton")) != at_0931_next(
        await predictions_of(without, "newton")
    )


def at_0931_next(predictions: list[dict[str, Any]]) -> tuple[Any, ...]:
    p = next(p for p in predictions if p["bar_close"] == "2026-10-12T09:32:00+09:00")
    return (p["p_up"], p["p_flat"], p["p_down"], p["expected_ticks"])
