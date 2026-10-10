"""Condorcet, the consensus, through the Replay Harness.

Input: market events, stand-in algorithms with fixed forecasts and chosen slowness, their
statuses, and scored history put in the prediction store. Output: the consensus
predictions the Engine API shows, and its weights (``/v1/consensus``).

Only active algorithms take part. The fast consensus is made from those finished soon
after the bar close (2 seconds live, shortened here); the final one once all have
finished or run out of time. Both are predictions of their own, stored and scored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from krx_quant.baselines import Forecast
from krx_quant.consensus import Timing
from krx_quant.lifecycle import Status
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import DAY, SYMBOL, api, at, get_json, run
from tests.test_lifecycle import morning, stored, weekdays_before
from tests.test_replay import busy_morning

QUICK = FixedAlgorithm(
    "quick", features=("velocity",), unique=("velocity",), forecast=Forecast(0.6, 0.3, 0.1, 1.0)
)
SLOW = FixedAlgorithm(
    "slow",
    features=("acceleration",),
    unique=("acceleration",),
    forecast=Forecast(0.2, 0.3, 0.5, -1.0),
    seconds=0.4,
)
NEW = FixedAlgorithm(
    "new", features=("relative_volume",), unique=("relative_volume",), forecast=Forecast(0.0, 0.0, 1.0, -5.0)
)
TIMING = Timing(fast_seconds=0.15)


def put_statuses(db: Path, statuses: dict[str, Status]) -> None:
    with stored(db) as store:
        for name, status in statuses.items():
            store.add_status(name, morning(weekdays_before(DAY, 30)[0]), None, status, "set by the test")


async def at_close(engine: Any, close: str) -> dict[str, dict[str, Any]]:
    """The predictions of the bar close at ``close``, by algorithm."""
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
    return {p["algorithm"]: p for p in body["predictions"] if p["bar_close"][11:16] == close}


async def test_the_fast_consensus_takes_the_active_finished_soon_and_the_final_takes_all(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    put_statuses(db, {"quick": Status.ACTIVE, "slow": Status.ACTIVE, "new": Status.PROBATION})

    engine = await run(
        db, busy_morning(), until=at("09:06:00"), algorithms=[QUICK, SLOW, NEW], consensus_timing=TIMING
    )

    made = await at_close(engine, "09:01")
    fast, final = made["consensus-fast"], made["consensus-final"]
    # Before 0.15 s only quick had finished; new is on probation, so never a member.
    assert (fast["p_up"], fast["p_flat"], fast["p_down"], fast["expected_ticks"]) == (0.6, 0.3, 0.1, 1.0)
    # No weights yet: the members count equally.
    assert (final["p_up"], final["p_flat"], final["p_down"], final["expected_ticks"]) == pytest.approx(
        (0.4, 0.3, 0.3, 0.0)
    )
    assert fast["model_version"] == final["model_version"] == "condorcet-equal"
    assert fast["outcome"] is not None and final["outcome"] is not None  # scored by 09:05


async def test_with_no_active_algorithm_the_consensus_is_recorded_empty(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("09:03:00"), algorithms=[QUICK])

    made = await at_close(engine, "09:01")
    for name in ("consensus-fast", "consensus-final"):
        assert (made[name]["status"], made[name]["p_up"], made[name]["outcome"]) == ("no_members", None, None)
