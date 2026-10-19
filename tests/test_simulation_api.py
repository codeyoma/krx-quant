"""Simulated probation in the Engine API (#298): ``/v1/algorithms`` (each algorithm's latest
simulation, its stage's simulated days, its status history marked), the stream's lifecycle events,
and the leaderboard that never counts the simulated days. Every answer is checked against the
schema."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.clock import FakeClock
from krx_quant.lifecycle import Status
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.simulation import simulate_daily
from krx_quant.touch import TARGET_VERSION
from tests.api_schema import check
from tests.replay_events import api, get_json
from tests.test_api_stream import engine_at, receive_until
from tests.test_simulation_daily import (
    LAST,
    NIGHT,
    AgainstTheNight,
    Flaky,
    engine_on,
    enroll,
    passed_report,
    settings,
)


@pytest.fixture(autouse=True)
def judged(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests judge open_to_close's algorithms, as the engine will once its close is fixed
    (#264, ``daily.JUDGING_HELD``)."""
    monkeypatch.setattr("krx_quant.daily.JUDGING_HELD", {})


async def listed(engine: Any) -> dict[str, Any]:
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
        check("algorithms", body)
        detail = await get_json(client, "/v1/algorithms/against")
        check("algorithm_detail", detail)
    return {a["name"]: a for a in body["algorithms"]}


async def test_a_simulation_that_did_not_pass_shows_in_the_stage_and_the_algorithm(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=7)
    against = AgainstTheNight()
    began = enroll(s, against.name)
    [sim] = simulate_daily(s, algorithms=[against], now=NIGHT).simulations
    assert sim.status is Status.PROBATION
    clock = FakeClock(datetime.combine(LAST + timedelta(days=3), time(7), NIGHT.tzinfo))
    e = engine_on(s, clock, [against])

    shown = (await listed(e))["against"]

    stage = shown["stage"]
    assert (stage["days"], stage["simulated"], stage["next_review"], stage["counted_from"]) == (
        20,
        20,
        40,
        None,
    )
    simulation = shown["simulation"]
    assert simulation["probation_began"] == began.isoformat()
    assert (simulation["cadence"], simulation["days"]) == ("daily", 20)
    assert (simulation["first_day"], simulation["last_day"]) == (sim.days[0].isoformat(), LAST.isoformat())
    assert len(simulation["training"]["models"]) == 20 and simulation["why"] is None
    review = simulation["review"]
    assert (review["review_day"], review["status"], review["reason"]) == (20, "probation", None)
    assert review["scored"] == 60 and review["enough"] is True
    [entered] = shown["status"]["history"]
    assert entered["simulated"] is False
    e.store.close()


async def test_a_simulation_that_passed_marks_the_status_change(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=4.0)
    passed_report(s)
    simulate_daily(s, now=NIGHT)
    marked = PredictionStore(s.state_dir / DB_NAME)
    marked.mark_target(TARGET_VERSION, NIGHT)  # the server's DB is on the touch target: no restart
    marked.close()
    e = engine_on(s, FakeClock(NIGHT + timedelta(hours=8)), [])

    async with api(e) as client:
        body = await get_json(client, "/v1/algorithms")
    check("algorithms", body)
    store = PredictionStore(s.state_dir / DB_NAME)
    try:
        history = store.status_history("cox")
        record = store.simulations()["cox"]
    finally:
        store.close()
    assert [h["simulated"] for h in history] == [False, True]
    assert record.detail["review"]["status"] == "active"
    e.store.close()


async def test_one_that_could_not_be_simulated_says_why(tmp_path: Path) -> None:
    s = settings(tmp_path, signal=0.0, seed=5)
    barren = Flaky("against")  # learns nothing on any night
    enroll(s, barren.name)
    simulate_daily(s, algorithms=[barren], now=NIGHT)
    e = engine_on(s, FakeClock(NIGHT + timedelta(hours=8)), [barren])

    simulation = (await listed(e))["against"]["simulation"]

    assert (simulation["days"], simulation["cadence"], simulation["training"], simulation["review"]) == (
        0,
        None,
        None,
        None,
    )
    assert simulation["why"].startswith("nothing to learn before")
    e.store.close()


async def test_a_simulated_status_change_is_streamed_marked(tmp_path: Path) -> None:
    engine, _ = engine_at(tmp_path / "engine.db", datetime(2026, 10, 12, 9, tzinfo=NIGHT.tzinfo))

    async with TestClient(TestServer(create_app(engine, stream_poll_seconds=0.01))) as client:
        ws = await client.ws_connect("/v1/stream")
        await asyncio.sleep(0.05)  # the stream is polling
        command = PredictionStore(tmp_path / "engine.db")  # `krx-quant simulate`, another process
        command.change_status(
            "second",
            datetime(2026, 10, 12, 23, tzinfo=NIGHT.tzinfo),
            Status.ACTIVE,
            Status.BENCH,
            "simulated probation 2026-09-14..2026-09-25 (10 days): test",
            simulated=True,
        )
        command.close()
        [event] = await receive_until(ws, lambda e: e["type"] == "lifecycle")
        await ws.close()

    assert event["change"]["simulated"] is True
    assert event["change"]["reason"].startswith("simulated probation")
