"""The ranking through the engine (#697): made at the daily review from the stored rows, the shadow ones
too, kept and logged; the verdict pools the latest ranking's members, a shadow member run before it.
Made-up forecasts and prices."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, UP_FIRST
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.lifecycle import Status
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.ranking import LIVE, RANKING_VERSION
from krx_quant.trader import SYMBOL
from tests.api_schema import check
from tests.ranking_doubles import seed_ranking
from tests.replay_events import DAY, api, at, get_json, run
from tests.test_verdict_engine import DIRECTION, DOWN, Leaning, morning

EARLIER = [DAY - timedelta(days=d) for d in range(18, 0, -1) if (DAY - timedelta(days=d)).weekday() < 5]


def seed_history(db: Path, name: str) -> None:
    """Twelve earlier days of ``name``'s barrier forecasts, three bar closes a day, 0.6 on the class
    that happened against the climate's third each; the model trained on the barrier's answers."""
    store = PredictionStore(db)
    third = (1 / 3, 1 / 3, 1 / 3)
    for day in EARLIER[-12:]:
        for minute, label in ((1, UP_FIRST), (2, DOWN_FIRST), (3, UP_FIRST)):
            t = datetime.combine(day, datetime.min.time(), KST) + timedelta(hours=10, minutes=minute)
            mine = (0.6, 0.2, 0.2) if label == UP_FIRST else (0.2, 0.2, 0.6)
            for who, p, lifecycle in ((CLIMATE, third, None), (name, mine, "shadow")):
                store.add_kind_predictions(
                    BARRIER.name,
                    [
                        NewKindPrediction(
                            SYMBOL,
                            who,
                            f"{who}-v",
                            t,
                            t,
                            dict(zip(BARRIER.p_keys(), p, strict=True)),
                            lifecycle=lifecycle,
                        )
                    ],
                )
            store.add_kind_answers(
                BARRIER.name, BARRIER.answer_version, [(SYMBOL, t, Answer(label, None, {}), False)]
            )
    store.record_models([(name, f"{name}-v", BARRIER.answer_version)])
    store.close()


async def test_the_daily_review_ranks_each_horizon_from_its_stored_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: twelve earlier days of ``lean-4``'s shadow forecasts beating the climate, then a morning
    and the day's review (15:40), ``lean-4`` waiting for its pre-validation (shadow), ``lean-15`` with
    no day before, ``lean-30`` retired. Output: the night's barrier ranking makes ``lean-4`` a member
    (the kind's first ranking), its status shadow, ranked on its live days; ``lean-15`` unranked (no
    evidence: its forecasts, of its defaults, are not on the target); ``lean-30`` not ranked; kept once,
    one log line a kind;
    ``GET /v1/ranking`` serves them within the contract; that morning's verdicts waited (no ranking
    before)."""
    caplog.set_level("INFO", logger="krx_quant.engine")
    db = tmp_path / "engine.db"
    seed_history(db, "lean-4")
    store = PredictionStore(db)
    store.add_status("lean-30", at("08:00:00"), None, Status.RETIRED, "retired by hand")
    store.close()
    members = [
        Leaning("lean-4", "barrier", "velocity", DOWN),
        Leaning("lean-15", "barrier_15m", "velocity", DOWN),
        Leaning("lean-30", "barrier_30m", "velocity", DOWN),
    ]
    engine = await run(
        db,
        morning(),
        until=at("15:45:00"),
        algorithms=[DIRECTION],
        kind_algorithms=members,
        admits=lambda a: a.name != "lean-4",
        ranks=True,
    )

    kept = {b["kind"]: b for b in engine.store.rankings(RANKING_VERSION)}
    four = kept["barrier"]
    assert four["night"] == DAY.isoformat() and four["members"] == ["lean-4"]
    row = four["rows"][0]
    assert (row["algorithm"], row["status"], row["source"], row["member"], row["rank"]) == (
        "lean-4",
        "shadow",
        LIVE,
        True,
        1,
    )
    # The twelve earlier days: the morning's forecasts, of its defaults, were not trained on the answers.
    assert row["days"] == 12 and row["skill_low"] > 0
    fifteen = kept["barrier_15m"]
    assert fifteen["members"] == [] and fifteen["rows"][0]["why"] == "no_evidence"
    assert kept["barrier_30m"]["rows"] == [], "a retired one is not ranked"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ranking barrier ")]
    assert lines == [
        f"ranking barrier {DAY}: members lean-4 | ranked 1. lean-4 {row['skill_low']:+.4f} "
        f"(live, 12d, {row['calls']} calls, member)"
    ]
    async with api(engine) as client:
        served = await get_json(client, "/v1/ranking")
        that_night = await get_json(client, f"/v1/ranking?night={DAY.isoformat()}")
    check("ranking", served)
    assert served == that_night
    assert {k["kind"] for k in served["kinds"]} >= {"barrier", "barrier_15m"}
    morning_verdict = engine.store.verdicts([SYMBOL], at("09:31:00"))[0]
    assert (morning_verdict["verdict"], morning_verdict["reason"]) == ("wait", "no_members")
    engine.store.close()


async def test_a_shadow_member_runs_before_the_verdict_and_is_pooled(tmp_path: Path) -> None:
    """Input: the night before ranked ``lean-4`` (waiting for its pre-validation: shadow) a member of the
    four minutes, and nothing of the fifteen; ``lean-15`` enrolled. Output: at 09:31 the four minutes
    pools ``lean-4``, status shadow, counted; ``lean-15`` is shown, not counted; SELL (avoid) on the four
    minutes."""
    db = tmp_path / "engine.db"
    seed_ranking(db, {"barrier": {"lean-4"}, "barrier_15m": set()})
    members = [
        Leaning("lean-4", "barrier", "velocity", DOWN),
        Leaning("lean-15", "barrier_15m", "velocity", DOWN),
    ]
    engine = await run(
        db,
        morning(),
        until=at("09:35:00"),
        algorithms=[DIRECTION],
        kind_algorithms=members,
        admits=lambda a: a.name != "lean-4",
    )

    async with api(engine) as client:
        found = (
            await get_json(client, f"/v1/verdict?symbol={SYMBOL}&bar_close=2026-10-12T09:31:00%2B09:00")
        )["verdicts"][0]
    check("verdict", {"version": 2, "verdicts": [found]})
    shown = {m["name"]: m for m in found["members"]}
    assert (shown["lean-4"]["status"], shown["lean-4"]["counted"], shown["lean-4"]["ranked"]) == (
        "shadow",
        True,
        True,
    )
    assert (shown["lean-15"]["counted"], shown["lean-15"]["ranked"]) == (False, False)
    assert (found["verdict"], found["meaning"], found["horizon"]["minutes"]) == ("sell", "avoid", 4)
    assert found["advisory"] == []  # no daily kind in a replay
    engine.store.close()
