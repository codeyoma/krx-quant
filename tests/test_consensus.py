"""Condorcet, the consensus, through the Replay Harness.

Input: market events, stand-in algorithms with fixed forecasts and chosen slowness, their
statuses, and scored history put in the prediction store. Output: the consensus
predictions the Engine API shows, and its weights (``/v1/consensus``).

Only active algorithms take part. The fast consensus is made from those finished soon
after the bar close (2 seconds live, shortened here); the final one once all have
finished or run out of time. Both are predictions of their own, stored and scored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from krx_quant.baselines import Forecast
from krx_quant.consensus import Timing
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import DAY, SYMBOL, api, at, get_json, run
from tests.test_lifecycle import (
    MISSES_DOWNS,
    NEXT_DAY,
    RIGHT,
    WRONG,
    morning,
    put_days,
    stored,
    weekdays_before,
)
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
ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
GAMMA = FixedAlgorithm("gamma", features=("relative_volume",), unique=("relative_volume",))
UPPER = FixedAlgorithm(
    "upper", features=("velocity",), unique=("velocity",), forecast=Forecast(0.6, 0.3, 0.1, 1.0)
)
LOWER = FixedAlgorithm(
    "lower", features=("acceleration",), unique=("acceleration",), forecast=Forecast(0.2, 0.3, 0.5, -1.0)
)
TWO_DAYS = MarketCalendar({DAY: True, NEXT_DAY: True})


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
    statuses = {"quick": Status.ACTIVE, "slow": Status.ACTIVE, "new": Status.PROBATION}
    statuses |= {"benched": Status.BENCH, "copy": Status.CLONE}
    put_statuses(db, statuses)
    others = [
        FixedAlgorithm(name, features=(f,), unique=(f,), forecast=Forecast(0.0, 0.0, 1.0, -5.0))
        for name, f in (("benched", "vwap_z"), ("copy", "reversion_speed"))
    ]

    engine = await run(
        db,
        busy_morning(),
        until=at("09:06:00"),
        algorithms=[QUICK, SLOW, NEW, *others],
        consensus_timing=TIMING,
    )

    made = await at_close(engine, "09:01")
    fast, final = made["consensus-fast"], made["consensus-final"]
    # Before 0.15 s only quick had finished. On probation, the bench or a clone, never a member.
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


async def weights_after_review(
    tmp_path: Path, days: int, calls: dict[str, list[Direction]], **kept: Any
) -> dict[str, float]:
    """The 09:00 slot's weights of 005930 after DAY's review, with ``calls`` on the ``days``
    weekdays before (alpha, beta, gamma active).
    """
    db = tmp_path / "engine.db"
    put_statuses(db, dict.fromkeys(calls, Status.ACTIVE))
    put_days(db, weekdays_before(DAY, days), calls)
    members = [a for a in (ALPHA, BETA, GAMMA) if a.name in calls]

    engine = await run(db, [], until=at("15:45:00"), algorithms=members)

    async with api(engine) as client:
        body = await get_json(client, "/v1/consensus")
    assert (body["version"], body["computed_at"]) == ("condorcet-20261012", "2026-10-12T15:40:00+09:00")
    assert body["members"] == list(calls)
    weights: dict[str, float] = body["weights"][SYMBOL]["09:00"]
    return weights


async def test_an_algorithm_better_on_every_span_gains_weight_as_the_evidence_grows(tmp_path: Path) -> None:
    # alpha is right at every bar close, beta misses the downs. Fitted on its own, the pool
    # would trust alpha alone. The 20-day span holds the replayed day (nothing scored) and
    # 19 days of 8 bar closes: n = 152, so the fit counts 152 / 352 and equal weights the rest.
    weights = await weights_after_review(tmp_path, 25, {"alpha": RIGHT, "beta": MISSES_DOWNS})

    trust = 152 / 352
    assert weights["alpha"] == pytest.approx(trust + (1 - trust) / 2, abs=1e-6)
    assert weights["beta"] == pytest.approx((1 - trust) / 2, abs=1e-6)


async def test_with_little_evidence_the_weights_stay_near_equal(tmp_path: Path) -> None:
    weights = await weights_after_review(tmp_path, 1, {"alpha": RIGHT, "beta": MISSES_DOWNS})

    trust = 8 / 208  # one day of 8 bar closes against 200 of belief in equal weights
    assert weights["alpha"] == pytest.approx(trust + (1 - trust) / 2, abs=1e-6)


async def test_an_algorithm_good_only_lately_gains_nothing(tmp_path: Path) -> None:
    # gamma was wrong for 20 days and right for the last 5; alpha missed the downs all along.
    # The 5-day span (the replayed day and four) has gamma the better, the 20-day span (14
    # wrong days and 5 right) alpha: neither is steady, so equal weights.
    days = 25
    gamma = [WRONG] * (days - 5) + [RIGHT] * 5
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": Status.ACTIVE, "gamma": Status.ACTIVE})
    history = weekdays_before(DAY, days)
    for day, gamma_calls in zip(history, gamma, strict=True):
        put_days(db, [day], {"alpha": MISSES_DOWNS, "gamma": gamma_calls})

    engine = await run(db, [], until=at("15:45:00"), algorithms=[ALPHA, GAMMA])

    async with api(engine) as client:
        body = await get_json(client, "/v1/consensus")
    assert body["weights"][SYMBOL]["09:00"] == pytest.approx({"alpha": 0.5, "gamma": 0.5})


async def test_the_consensus_uses_the_weights_refitted_after_the_close(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    put_statuses(db, {"upper": Status.ACTIVE, "lower": Status.ACTIVE})
    put_days(db, weekdays_before(DAY, 25), {"upper": RIGHT, "lower": MISSES_DOWNS})
    await run(db, [], until=at("15:45:00"), algorithms=[UPPER, LOWER])  # DAY's review fits them

    engine = await run(
        db,
        busy_morning(NEXT_DAY),
        until=at("09:02:00", NEXT_DAY),
        calendar=TWO_DAYS,
        algorithms=[UPPER, LOWER],
    )

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=consensus-final")
    [final] = [p for p in body["predictions"] if p["bar_close"] == "2026-10-13T09:01:00+09:00"]
    trust = 152 / 352
    w_upper = trust + (1 - trust) / 2
    assert final["model_version"] == "condorcet-20261012"
    assert final["p_up"] == pytest.approx(w_upper * 0.6 + (1 - w_upper) * 0.2)


async def test_with_three_members_only_the_steady_one_takes_what_the_weak_one_loses(tmp_path: Path) -> None:
    # alpha is right all along; beta only for the last four days; gamma never on the moves.
    # Over 5 days (the replayed day and four) the pool would split alpha and beta, over 20
    # alpha alone. alpha is above a third on both spans (it keeps the smaller, 1/2), beta is
    # above on one and below on the other (it stays at a third), gamma below on both (about
    # 0). What gamma gives up goes to alpha only: 2/3, 1/3 and 0 before the shrink.
    db = tmp_path / "engine.db"
    put_statuses(db, dict.fromkeys(("alpha", "beta", "gamma"), Status.ACTIVE))
    history = weekdays_before(DAY, 25)
    for i, day in enumerate(history):
        beta = RIGHT if i >= len(history) - 4 else WRONG
        put_days(db, [day], {"alpha": RIGHT, "beta": beta, "gamma": WRONG})

    engine = await run(db, [], until=at("15:45:00"), algorithms=[ALPHA, BETA, GAMMA])

    async with api(engine) as client:
        weights = (await get_json(client, "/v1/consensus"))["weights"][SYMBOL]["09:00"]
    trust = 152 / 352
    assert weights["beta"] == pytest.approx(1 / 3, abs=1e-6)
    assert weights["alpha"] == pytest.approx(trust * 2 / 3 + (1 - trust) / 3, abs=1e-4)
    assert weights["gamma"] == pytest.approx((1 - trust) / 3, abs=1e-4)


class Failing(FixedAlgorithm):
    def predict(self, features: Any, model: Any) -> Any:
        raise ValueError("a bug")


async def test_members_that_have_not_finished_or_failed_leave_the_consensus_without_a_forecast(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    failing = Failing("failing", features=("relative_volume",), unique=("relative_volume",))
    put_statuses(db, {"slow": Status.ACTIVE, "failing": Status.ACTIVE})

    engine = await run(
        db, busy_morning(), until=at("09:02:00"), algorithms=[SLOW, failing], consensus_timing=TIMING
    )

    made = await at_close(engine, "09:01")
    assert (made["consensus-fast"]["status"], made["consensus-fast"]["p_up"]) == ("none_finished", None)
    assert (made["consensus-final"]["status"], made["consensus-final"]["p_up"]) == ("ok", 0.2)  # slow alone


async def test_the_final_consensus_does_not_wait_for_algorithms_outside_it(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    late = FixedAlgorithm("late", features=("acceleration",), unique=("acceleration",), seconds=0.4)
    put_statuses(db, {"quick": Status.ACTIVE, "late": Status.PROBATION})

    await run(db, busy_morning(), until=at("09:01:30"), algorithms=[QUICK, late], consensus_timing=TIMING)

    with sqlite3.connect(db) as conn:
        order = [
            r[0] for r in conn.execute("SELECT algorithm FROM predictions WHERE status = 'ok' ORDER BY id")
        ]
    assert order.index("consensus-final") < order.index("late")  # stored while late still worked


async def test_the_weights_are_fitted_on_what_happened_from_the_bar_close(tmp_path: Path) -> None:
    # On basis B each member is scored from its own display; the pool needs one outcome per
    # bar close, so the fit takes basis A's. Here B saw only flats (displays after the
    # moves): fitted on B, the always-flat beta would win; on A, alpha is the right one.
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": Status.ACTIVE, "beta": Status.ACTIVE})
    flat = [Direction.FLAT] * len(RIGHT)
    put_days(db, weekdays_before(DAY, 25), {"alpha": RIGHT, "beta": flat}, on_display=Direction.FLAT)

    engine = await run(db, [], until=at("15:45:00"), algorithms=[ALPHA, BETA])

    async with api(engine) as client:
        weights = (await get_json(client, "/v1/consensus"))["weights"][SYMBOL]["09:00"]
    assert weights["alpha"] > 0.5 > weights["beta"]


async def test_a_member_the_fit_did_not_know_gets_an_equal_share(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    put_statuses(db, {"upper": Status.ACTIVE, "lower": Status.ACTIVE})
    put_days(db, weekdays_before(DAY, 25), {"upper": RIGHT, "lower": MISSES_DOWNS})
    await run(db, [], until=at("15:45:00"), algorithms=[UPPER, LOWER])  # fits upper and lower
    third = FixedAlgorithm(
        "third", features=("vwap_z",), unique=("vwap_z",), forecast=Forecast(0.4, 0.4, 0.2, 0.0)
    )
    put_statuses(db, {"third": Status.ACTIVE})  # active since, unknown to the fit

    engine = await run(
        db,
        busy_morning(NEXT_DAY),
        until=at("09:02:00", NEXT_DAY),
        calendar=TWO_DAYS,
        algorithms=[UPPER, LOWER, third],
    )

    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm=consensus-final")
    [final] = [p for p in body["predictions"] if p["bar_close"] == "2026-10-13T09:01:00+09:00"]
    trust = 152 / 352
    w_upper = trust + (1 - trust) / 2  # as fitted, between the two it knew
    fitted_part = w_upper * 0.6 + (1 - w_upper) * 0.2
    assert final["p_up"] == pytest.approx(2 / 3 * fitted_part + 1 / 3 * 0.4)
