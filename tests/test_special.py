"""Special situations through the Replay Harness: VIs, circuit breakers and sidecars.

Input: hand-made events (a wandering book and trades, VI status answers, the KOSPI index
and KOSPI200 futures) and a fake clock. Output: what the Engine API shows and the
leaderboard counts.

A VI is known when its status answer is received (polled every few seconds): from then
on, a bar close inside it is not predicted, and a prediction whose four minutes it
touches is special, even one made before it was known.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.replay_events import PREDICTORS, SYMBOL, api, at, get_json, run, vi
from tests.test_replay import busy_morning

# A VI from 09:30:20 to 09:32:40, seen on by 09:30:24 and released by 09:32:44.
VI = [vi(at("09:30:24"), "09:30:20"), vi(at("09:32:44"), "09:30:20", "09:32:40")]


async def predictions(engine: Any) -> dict[str, list[dict[str, Any]]]:
    """Every prediction of the symbol, by bar close ("09:31")."""
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
    by_close: dict[str, list[dict[str, Any]]] = {}
    for p in body["predictions"]:
        by_close.setdefault(p["bar_close"][11:16], []).append(p)
    return by_close


async def test_bar_closes_inside_a_vi_are_not_predicted(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [*busy_morning(), *VI], until=at("09:40:00"))

    made = await predictions(engine)
    for close in ("09:31", "09:32"):
        assert {p["algorithm"] for p in made[close]} == PREDICTORS
        assert {(p["status"], p["p_up"], p["outcome"]) for p in made[close]} == {("vi", None, None)}
    for close in ("09:30", "09:33"):  # the VI was not yet known; it was over
        assert {p["status"] for p in made[close]} == {"ok"}


def special(made: dict[str, list[dict[str, Any]]], close: str) -> set[bool]:
    return {p["outcome"]["special"] for p in made[close] if p["outcome"] is not None}


async def test_a_vi_touching_a_predictions_four_minutes_makes_it_special(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [*busy_morning(), *VI], until=at("09:45:00"))

    made = await predictions(engine)
    # 09:27–09:31 reaches into the VI; 09:26–09:30 ends before it; 09:33 starts after it.
    for close in ("09:27", "09:28", "09:29", "09:30"):
        assert special(made, close) == {True}, close
    for close in ("09:26", "09:33"):
        assert special(made, close) == {False}, close


async def test_a_vi_learned_late_still_makes_what_it_touched_special(tmp_path: Path) -> None:
    # The status polls failed until 09:34: the VI is first seen over, after 09:31 and 09:32
    # were predicted and 09:27 was scored.
    late = [vi(at("09:34:05"), "09:30:20", "09:32:40")]

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *late], until=at("09:45:00"))

    made = await predictions(engine)
    assert {p["status"] for p in made["09:31"]} == {"ok"}
    for close in ("09:27", "09:30", "09:31", "09:32"):
        assert special(made, close) == {True}, close
    assert special(made, "09:33") == {False}


async def test_special_predictions_are_left_out_of_the_leaderboard_by_default(tmp_path: Path) -> None:
    with_vi = await run(tmp_path / "vi.db", [*busy_morning(), *VI], until=at("09:45:00"))
    without = await run(tmp_path / "plain.db", busy_morning(), until=at("09:45:00"))

    async def flat_n(engine: Any, query: str = "") -> int:
        async with api(engine) as client:
            body = await get_json(client, f"/v1/leaderboard?basis=a{query}")
        [flat] = [b for b in body["baselines"] if b["algorithm"] == "baseline-flat"]
        return int(flat["n"])

    # 09:01–09:41 are scored by 09:45. With the VI, 09:31 and 09:32 were not predicted and
    # 09:27–09:30 are special.
    assert await flat_n(without) == 41
    assert await flat_n(with_vi) == 41 - 2 - 4
    assert await flat_n(with_vi, "&special=include") == 41 - 2
