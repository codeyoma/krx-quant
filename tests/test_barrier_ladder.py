"""The barrier's horizon ladder (#263): the barrier with a time barrier of 15, 30 and 60 minutes.

Input: the ladder as registered; the bars of +3 and −3 ticks by 09:37 (``test_barriers``); the
volatile morning (``test_barriers.volatile_morning``) replayed with the kinds the engine
predicts. Output: each rung's name, horizon and baselines; its width σ̂₁ × √H; the answers of
its own windows; its persistence reading the window one horizon back; its climate's own table.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms.contract import Model
from krx_quant.barriers import (
    BARRIER,
    BARRIER_LADDER,
    CLIMATE,
    LADDER_MINUTES,
    PERSIST,
    BarrierClimate,
    BarrierPersist,
)
from krx_quant.features import Snapshot, Width
from krx_quant.kind_registry import KINDS
from krx_quant.predictions import PredictionStore
from krx_quant.touch import SILENT, TARGET_VERSION
from tests.api_schema import check
from tests.replay_events import FEE, SYMBOL, api, at, get_json, run
from tests.test_barriers import two_changes, volatile_morning

RUNGS = {k.name: k for k in BARRIER_LADDER}


def test_the_ladder_is_the_barrier_at_fifteen_thirty_and_sixty_minutes() -> None:
    """Each rung is the barrier but for its horizon, its name and its baselines' names; the
    four-minute barrier keeps its own."""
    assert LADDER_MINUTES == (15, 30, 60)
    assert [(k.name, k.title, k.horizon.total_seconds() / 60) for k in BARRIER_LADDER] == [
        ("barrier_15m", "장벽 15분", 15),
        ("barrier_30m", "장벽 30분", 30),
        ("barrier_60m", "장벽 60분", 60),
    ]
    for k in BARRIER_LADDER:
        m = int(k.horizon.total_seconds() // 60)
        same = (k.classes, k.class_titles, k.action, k.answer_version, k.crossed, k.p_keys())
        assert same == (
            BARRIER.classes,
            BARRIER.class_titles,
            BARRIER.action,
            BARRIER.answer_version,
            BARRIER.crossed,
            BARRIER.p_keys(),
        )
        assert (k.answer, k.bar_answer, k.stats) == (BARRIER.answer, BARRIER.bar_answer, BARRIER.stats)
        assert [b.name for b in k.baselines] == [f"{CLIMATE}-{m}m", f"{PERSIST}-{m}m"]
        assert k in KINDS
    assert BARRIER.horizon.total_seconds() == 240
    assert [b.name for b in BARRIER.baselines] == [CLIMATE, PERSIST]
    names = [b.name for k in KINDS for b in k.baselines]
    assert len(names) == len(set(names)), "every baseline its own name"


def test_a_rung_s_baselines_say_their_minutes() -> None:
    climate, persist = BarrierClimate(minutes=30), BarrierPersist(30)
    assert climate.card.character == "기준 예측: 장벽 30분 시간대 기후"
    assert "`barrier-climate-30m-<날짜>-<해시>`" in climate.card.forecasting
    assert climate.version == climate.default_table().version == "barrier-climate-30m-default"
    installed = climate.using(Model("barrier-climate-30m-20261016-abcdef", {}))
    assert (installed.name, installed.minutes, installed.version) == (
        "barrier-climate-30m",
        30,
        "barrier-climate-30m-20261016-abcdef",
    )
    assert persist.card.character == "기준 예측: 직전 30분 장벽"
    assert "직전 30분(t − 30분 → t)" in persist.card.logic
    assert BarrierClimate().card.character == "기준 예측: 장벽 시간대 기후"
    assert BarrierClimate().version == f"{CLIMATE}-default"


def test_a_rung_s_width_is_the_one_minute_deviation_times_the_root_of_its_minutes() -> None:
    """Input: the bars of +3 and −3 by 09:37 (σ̂₁ = 3√2). Output: w = 8 at four minutes (6√2,
    the snapshot's own), 16 at 15 (16.4), 23 at 30 (23.2), 33 at 60 (32.9); with too few bars
    w = k = 2 at every rung."""

    def snapshot(fillers: int = 16) -> Snapshot:
        bars = two_changes(3, -3, fillers=fillers)
        return Snapshot.at(SYMBOL, at("09:37:00"), bars, 81_250.0, FEE, session_open=at("09:00:00"))

    one = 3 * math.sqrt(2)
    assert BARRIER.width is not None
    four = BARRIER.width(snapshot())
    assert four is not None and four.ticks == 8 and four.sigma == pytest.approx(one * 2)
    widths = {}
    for k in BARRIER_LADDER:
        assert k.width is not None
        width = k.width(snapshot())
        assert width is not None and width.sigma is not None
        assert width.sigma == pytest.approx(one * math.sqrt(k.horizon.total_seconds() / 60))
        widths[k.name] = width.ticks
        assert k.width(snapshot(fillers=10)) == Width(2, None)
    assert widths == {"barrier_15m": 16, "barrier_30m": 23, "barrier_60m": 33}


def rows(db: Path, kind: str) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """A rung's answers by HH:MM and its predictions by (HH:MM, predictor)."""
    store = PredictionStore(db)
    try:
        answers = {a["bar_close"][11:16]: a for a in store.kind_answers(kind, SYMBOL)}
        made = store.kind_recent(kind, RUNGS[kind].answer_version, SYMBOL, 1000)
    finally:
        store.close()
    return answers, {(r["bar_close"][11:16], r["algorithm"]): r for r in made}


async def test_the_ladder_predicts_and_is_answered_on_its_own_windows(tmp_path: Path) -> None:
    """Input: the volatile morning to 09:55: w = k = 2 to 09:36, then σ̂₁ = 3√2; the bid at
    81,500 from 09:38:30 to 09:38:50, the ask at 80,400 from 09:44:00 to 09:44:20, no quote from
    09:47:00 to 09:48:10. Output: each rung keeps its bar close's width (2, then 16, 23, 33). The
    15-minute windows: 09:23's ends at 09:38 before the bid reaches 81,450 (timeout); 09:24's to
    09:32's reach it at 09:38:30 (up first, before the ask's dip); 09:33's to 09:40's went
    silent; none later is over. No 60-minute window is over. The persistence reads the window
    one horizon back: none in the first 15 minutes, 09:23's timeout at 09:38, 09:24's up first
    at 09:39, 09:33's silence at 09:48 (none); at 30 minutes, 09:09's up first at 09:39."""
    db = tmp_path / "engine.db"
    engine = await run(db, volatile_morning(), until=at("09:55:00"), target_version=TARGET_VERSION)

    fifteen, made = rows(db, "barrier_15m")
    assert fifteen["09:23"]["class"] == "timeout"
    assert [fifteen[f"09:{m}"]["class"] for m in range(24, 33)] == ["up_first"] * 9
    assert fifteen["09:30"]["path"]["up_at"] == at("09:38:30").isoformat()
    assert {fifteen[f"09:{m}"]["unscorable"] for m in range(33, 41)} == {SILENT}
    assert max(fifteen) == "09:40"
    assert rows(db, "barrier_60m")[0] == {}
    for kind, wide in (("barrier_15m", 16), ("barrier_30m", 23), ("barrier_60m", 33)):
        widths = {
            t: {p["width_ticks"] for (c, _), p in rows(db, kind)[1].items() if c == t}
            for t in ("09:36", "09:37")
        }
        assert widths == {"09:36": {2}, "09:37": {wide}}, kind
    for (t, _), p in made.items():
        if p["answer_path"]:
            assert p["answer_path"]["w"] == p["width_ticks"], t

    persist = f"{PERSIST}-15m"
    assert made[("09:15", persist)]["status"] == "no_previous"
    assert made[("09:38", persist)]["output"] == {"p_up_first": 0.0, "p_timeout": 1.0, "p_down_first": 0.0}
    assert made[("09:39", persist)]["output"]["p_up_first"] == 1.0
    assert made[("09:48", persist)]["status"] == "no_previous", "the 09:33 window went silent"
    assert rows(db, "barrier_30m")[1][("09:39", f"{PERSIST}-30m")]["output"]["p_up_first"] == 1.0
    assert made[("09:31", f"{CLIMATE}-15m")]["model_version"] == f"{CLIMATE}-15m-default"

    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?kind=barrier_15m")
        kinds = await get_json(client, "/v1/kinds")
        at_0937 = await get_json(
            client,
            f"/v1/symbols/{SYMBOL}/kinds/barrier_30m/predictions?bar_close=2026-10-12T09:37:00%2B09:00",
        )
    check("kind_leaderboard", board)
    check("kinds", kinds)
    check("kind_predictions", at_0937)
    entries = {e["algorithm"]: e for e in board["baselines"]}
    assert set(entries) == {f"{CLIMATE}-15m", persist}
    for entry in entries.values():
        check("barrier_stats", entry["extra"])
    listed = {k["name"]: (k["title"], k["horizon_minutes"]) for k in kinds["kinds"]}
    assert {n: listed[n] for n in ("barrier", *RUNGS)} == {
        "barrier": ("장벽", 4),
        "barrier_15m": ("장벽 15분", 15),
        "barrier_30m": ("장벽 30분", 30),
        "barrier_60m": ("장벽 60분", 60),
    }
    assert {p["target_at"] for p in at_0937["predictions"]} == {"2026-10-12T10:07:00+09:00"}
    assert {p["width_ticks"] for p in at_0937["predictions"]} == {23}
    engine.store.close()
