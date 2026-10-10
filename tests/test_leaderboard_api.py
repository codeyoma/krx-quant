"""The leaderboard and algorithm stats in the Engine API.

Input: scored predictions stored through the prediction store. Output: the API's
leaderboard and per-algorithm stats.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.baselines import Forecast
from krx_quant.engine import PredictionEngine
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from tests.replay_events import DAY, at, run
from tests.test_replay import busy_morning

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
# What happened at the 40 bar closes from 09:01: up, flat, down, flat, ...
OUTCOMES = [UP, FLAT, DOWN, FLAT] * 10
EARLIER = date(2026, 10, 8)


def store_scored(
    store: PredictionStore,
    algorithm: str,
    calls: Sequence[Direction],
    *,
    symbol: str = "005930",
    day: date = DAY,
    version: str = "v1",
    outcomes_b: Sequence[Direction] | None = None,
    special: bool = False,
) -> None:
    """Store ``calls`` (one sure call per bar close from 09:01), scored against OUTCOMES."""
    start = datetime.combine(day, at("09:01:00").timetz())
    for i, call in enumerate(calls):
        t = start + timedelta(minutes=i)
        [pending] = store.add_all(
            [
                NewPrediction(
                    symbol,
                    algorithm,
                    version,
                    t,
                    t,
                    SURE[call],
                    t + timedelta(minutes=4),
                    81_250,
                    81_250,
                    2,
                    special=special,
                )
            ]
        ).pending
        a = OUTCOMES[i]
        b = outcomes_b[i] if outcomes_b is not None else a
        target = 81_250 + {UP: 300, FLAT: 0, DOWN: -300}[a]
        store.score_all([(pending.prediction_id, Score(target, a, b, None))], t + timedelta(minutes=4))


@contextlib.asynccontextmanager
async def board(
    tmp_path: Path, min_sample: int = 20
) -> AsyncIterator[tuple[PredictionEngine, TestClient[Any, Any]]]:
    # A holiday: the engine predicts nothing itself; the test stores what it needs.
    engine = await run(
        tmp_path / "engine.db",
        [],
        until=at("16:00:00"),
        calendar=MarketCalendar({DAY: False}),
        symbols=("005930", "000660"),
    )
    async with TestClient(TestServer(create_app(engine, min_sample=min_sample))) as client:
        yield engine, client


async def get(client: TestClient[Any, Any], path: str, status: int = 200) -> Any:
    response = await client.get(path)
    assert response.status == status, await response.text()
    return await response.json()


def baselines(store: PredictionStore, n: int = 40, **kw: Any) -> None:
    store_scored(store, "baseline-flat", [FLAT] * n, **kw)
    store_scored(store, "baseline-persist", [UP] * n, **kw)
    store_scored(store, "baseline-random", [DOWN] * n, **kw)


async def test_the_leaderboard_ranks_by_the_lower_bound_of_the_weakest_lift(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES)  # always right
        store_scored(engine.store, "beta", [FLAT if o is DOWN else o for o in OUTCOMES])  # misses downs
        store_scored(engine.store, "gamma", OUTCOMES[:10])  # right, but too few to rank

        body = await get(client, "/v1/leaderboard")

    assert (body["symbol"], body["period"], body["basis"], body["min_sample"]) == ("all", "today", "b", 20)
    assert body["days"] == ["2026-10-12"]
    assert [(e["rank"], e["algorithm"]) for e in body["ranked"]] == [(1, "alpha"), (2, "beta")]
    alpha = body["ranked"][0]
    assert alpha["n"] == 40
    assert alpha["accuracy"]["value"] == 1.0
    # Always-flat is right on the 20 flats: alpha gains 0.5 at every other bar close.
    vs_flat = alpha["lift"]["vs"]["baseline-flat"]
    assert (vs_flat["n"], vs_flat["value"]) == (40, 0.5)
    assert vs_flat["q"] is not None and vs_flat["significant"] is True
    assert alpha["lift"]["low"] == min(v["low"] for v in alpha["lift"]["vs"].values())
    unranked = {e["algorithm"]: e for e in body["unranked"]}
    assert (unranked["gamma"]["n"], unranked["gamma"]["rank"]) == (10, None)
    # A registered algorithm with nothing scored yet is listed too.
    assert unranked["newton"]["n"] == 0 and unranked["newton"]["accuracy"] is None
    assert {b["algorithm"] for b in body["baselines"]} == {
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
    }
    assert all("lift" not in b for b in body["baselines"])


async def test_the_leaderboard_chooses_basis_symbol_period_and_leaves_out_special(tmp_path: Path) -> None:
    async with board(tmp_path, min_sample=1) as (engine, client):
        baselines(engine.store)
        # On basis B every outcome looked flat: alpha's directional calls missed.
        store_scored(engine.store, "alpha", OUTCOMES, outcomes_b=[FLAT] * 40)
        store_scored(engine.store, "alpha", OUTCOMES[:8], symbol="000660")
        store_scored(engine.store, "alpha", OUTCOMES[:4], day=EARLIER)
        store_scored(engine.store, "delta", OUTCOMES[:4], special=True)

        on_b = await get(client, "/v1/leaderboard")
        on_a = await get(client, "/v1/leaderboard?basis=a&symbol=005930")
        five_days = await get(client, "/v1/leaderboard?basis=a&symbol=005930&period=5d")
        with_special = await get(client, "/v1/leaderboard?special=include")

    def alpha(body: Any) -> Any:
        return next(e for e in body["ranked"] + body["unranked"] if e["algorithm"] == "alpha")

    assert alpha(on_b)["n"] == 48  # both symbols, today
    assert alpha(on_b)["accuracy"]["value"] == pytest.approx((20 + 8) / 48)
    assert alpha(on_a)["n"] == 40 and alpha(on_a)["accuracy"]["value"] == 1.0
    assert five_days["days"] == ["2026-10-12", "2026-10-08"]
    assert alpha(five_days)["n"] == 44
    assert all(e["algorithm"] != "delta" for e in on_b["ranked"] + on_b["unranked"])
    assert any(e["algorithm"] == "delta" for e in with_special["ranked"] + with_special["unranked"])


async def test_an_algorithms_stats_break_down_by_symbol_slot_and_model_version(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES[:30], version="alpha-1")
        store_scored(engine.store, "alpha", OUTCOMES[:30], symbol="000660", version="alpha-2")

        body = await get(client, "/v1/algorithms/alpha/stats?basis=a")

    assert body["overall"]["n"] == 60
    assert set(body["by_symbol"]) == {"005930", "000660"}
    # Bar closes 09:01–09:30 are the 09:00 slot (bars starting 09:00–09:29); 09:31 is the next.
    assert {slot: e["n"] for slot, e in body["by_slot"].items()} == {"09:00": 60}
    assert {v: e["n"] for v, e in body["by_model_version"].items()} == {"alpha-1": 30, "alpha-2": 30}
    assert len(body["overall"]["calibration"]) == 10
    assert body["overall"]["pnl"]["trades"] == 30
    # Overall lifts are adjusted with the whole leaderboard's; breakdowns are not tested.
    assert body["overall"]["lift"]["vs"]["baseline-flat"]["significant"] is True
    assert body["overall"]["lift"]["vs"]["baseline-flat"]["q"] is not None
    by_symbol = body["by_symbol"]["005930"]["lift"]["vs"]["baseline-flat"]
    assert (by_symbol["q"], by_symbol["significant"]) == (None, None)


async def test_bad_choices_and_unknown_algorithms_are_refused(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        store_scored(engine.store, "retired", OUTCOMES[:4], day=EARLIER)
        for query in ("period=week", "basis=c", "symbol=999999", "special=maybe"):
            assert "error" in await get(client, f"/v1/leaderboard?{query}", status=400)
        assert "error" in await get(client, "/v1/algorithms/einstein/stats", status=404)
        # Known from earlier days: today it simply has nothing.
        assert (await get(client, "/v1/algorithms/retired/stats"))["overall"]["n"] == 0


async def test_ranking_needs_enough_pairs_and_only_candidates_share_the_adjustment(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store, n=10)  # baselines only at the first 10 bar closes
        store_scored(engine.store, "alpha", OUTCOMES)  # 40 scored, but 10 pairs
        store_scored(engine.store, "beta", OUTCOMES[:5])  # too few to be a candidate

        body = await get(client, "/v1/leaderboard")

    assert body["ranked"] == []
    unranked = {e["algorithm"]: e for e in body["unranked"]}
    assert unranked["alpha"]["n"] == 40
    assert unranked["alpha"]["lift"]["vs"]["baseline-flat"]["n"] == 10
    assert unranked["beta"]["lift"]["vs"]["baseline-flat"]["q"] is None


async def test_a_replayed_morning_reaches_the_leaderboard(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", busy_morning(), until=at("10:00:00"))

    async with TestClient(TestServer(create_app(engine, min_sample=50))) as client:
        body = await get(client, "/v1/leaderboard?basis=a")
        newton = await get(client, "/v1/algorithms/newton/stats?basis=a")

    # 09:01–09:56 are scored by 10:00 (09:57–10:00 wait for their targets).
    assert {(e["algorithm"], e["n"]) for e in body["ranked"]} == {
        ("newton", 56),
        ("gauss", 56),
        ("granger", 56),
        ("markov", 56),
    }
    for ranked in body["ranked"]:
        assert set(ranked["lift"]["vs"]) == {"baseline-flat", "baseline-persist", "baseline-random"}
    assert [b["n"] for b in body["baselines"]] == [56, 56, 56]
    assert newton["overall"]["n"] == 56
    assert set(newton["by_slot"]) == {"09:00", "09:30"}


async def test_new_scores_show_on_the_next_request(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES[:30])
        before = await get(client, "/v1/leaderboard")
        store_scored(engine.store, "alpha", OUTCOMES[:10], symbol="000660")
        after = await get(client, "/v1/leaderboard")

    def alpha_n(body: Any) -> int:
        return next(int(e["n"]) for e in body["ranked"] + body["unranked"] if e["algorithm"] == "alpha")

    assert (alpha_n(before), alpha_n(after)) == (30, 40)
