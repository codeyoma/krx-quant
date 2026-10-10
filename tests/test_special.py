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

from krx_quant.consensus import NAMES as CONSENSUS
from tests.replay_events import PREDICTORS, SYMBOL, api, at, every, get_json, kospi, kospi200_futures, run, vi
from tests.test_replay import busy_morning

# A VI from 09:30:20 to 09:32:40, seen on by 09:30:24 and released by 09:32:44.
VI = [vi(at("09:30:24"), "09:30:20"), vi(at("09:32:44"), "09:30:20", "09:32:40")]


def made_by(predictions: list[dict[str, Any]]) -> set[str]:
    """The statuses of the baselines' and algorithms' predictions (the consensus is empty while
    no algorithm is active).
    """
    return {p["status"] for p in predictions if p["algorithm"] not in CONSENSUS}


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
        assert made_by(made[close]) == {"ok"}


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
    assert made_by(made["09:31"]) == {"ok"}
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


def held(start: str, end: str, make: Any, change_pct: float) -> list[Any]:
    """Ticks every 5 seconds from ``start`` to ``end``, all ``change_pct`` off the previous close."""
    return every(at(start), at(end), 5, lambda when: make(when, change_pct))


async def test_a_kospi_drop_of_8_percent_held_a_minute_is_a_circuit_breaker(tmp_path: Path) -> None:
    # Down 8.2% from 09:40:00: a circuit breaker at 09:41:00, trading halted 20 minutes and
    # then a 10-minute single-price auction, to 10:11. A dip at 09:20 lasted 40 seconds.
    market = [
        *held("09:20:00", "09:20:40", kospi, -8.5),
        *held("09:20:45", "09:39:55", kospi, -7.0),
        *held("09:40:00", "10:19:55", kospi, -8.2),
        # Down 15.4% from 10:20: the second stage at 10:21, to 10:51.
        *held("10:20:00", "10:22:00", kospi, -15.4),
    ]

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *market], until=at("10:30:00"))

    made = await predictions(engine)
    for close in ("09:17", "09:20", "09:37"):  # the dip; 09:37–09:41 ends as the halt begins
        assert special(made, close) == {False}, close
    for close in ("09:38", "09:41", "10:00", "10:10"):
        assert special(made, close) == {True}, close
    assert special(made, "10:16") == {False}  # between the stages
    assert special(made, "10:20") == {True}


async def test_kospi200_futures_5_percent_off_held_a_minute_is_a_sidecar_once_a_day(tmp_path: Path) -> None:
    # Up 5.3% from 09:20:00: a sidecar at 09:21:00 halts program trading for 5 minutes.
    # Up again at 09:40 for two minutes: a second sidecar the same day is not called.
    futures = [
        *held("09:20:00", "09:22:00", kospi200_futures, 5.3),
        *held("09:22:05", "09:39:55", kospi200_futures, 4.0),
        *held("09:40:00", "09:42:00", kospi200_futures, 5.6),
    ]

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *futures], until=at("09:55:00"))

    made = await predictions(engine)
    for close in ("09:18", "09:21", "09:25"):
        assert special(made, close) == {True}, close
    for close in ("09:17", "09:26", "09:41"):
        assert special(made, close) == {False}, close


async def test_a_vi_whose_release_is_not_seen_lasts_three_minutes(tmp_path: Path) -> None:
    on = [vi(at("09:30:24"), "09:30:20")]  # the release never comes through

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *on], until=at("09:40:00"))

    made = await predictions(engine)
    for close in ("09:31", "09:32", "09:33"):  # to 09:33:20
        assert {p["status"] for p in made[close]} == {"vi"}, close
    assert made_by(made["09:34"]) == {"ok"}


async def test_a_vi_at_the_open_skips_the_first_bar_closes(tmp_path: Path) -> None:
    at_open = [vi(at("09:00:14"), "09:00:10"), vi(at("09:02:39"), "09:00:10", "09:02:35")]

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *at_open], until=at("09:10:00"))

    made = await predictions(engine)
    assert {p["status"] for p in made["09:01"] + made["09:02"]} == {"vi"}
    assert made_by(made["09:03"]) == {"ok"}


async def test_a_futures_gap_from_before_the_open_is_a_sidecar_five_minutes_in(tmp_path: Path) -> None:
    # KOSPI200 futures trade from 08:45, already 6% up. The exchange calls no sidecar in the
    # first 5 minutes of the stock market, so the minute held starts at 09:05: 09:06–09:11.
    gap = held("08:45:00", "09:30:00", kospi200_futures, 6.0)

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *gap], until=at("09:30:00"))

    made = await predictions(engine)
    for close in ("09:02", "09:11"):
        assert special(made, close) == {False}, close
    for close in ("09:03", "09:10"):
        assert special(made, close) == {True}, close


async def test_no_breaker_or_sidecar_in_the_last_40_minutes(tmp_path: Path) -> None:
    late = [*held("14:51:00", "14:58:00", kospi, -8.5), *held("14:51:00", "14:58:00", kospi200_futures, -5.5)]

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *late], until=at("15:10:00"))

    made = await predictions(engine)
    assert special(made, "14:55") == {False}


async def test_the_status_shows_the_special_situations_known_today(tmp_path: Path) -> None:
    market = held("09:40:00", "09:42:00", kospi, -8.2)

    engine = await run(tmp_path / "engine.db", [*busy_morning(), *VI, *market], until=at("09:50:00"))

    async with api(engine) as client:
        status = await get_json(client, "/v1/status")
    assert status["special"] == [
        {
            "kind": "vi",
            "symbol": SYMBOL,
            "start": "2026-10-12T09:30:20+09:00",
            "end": "2026-10-12T09:32:40+09:00",
        },
        {
            "kind": "circuit_breaker",
            "symbol": None,
            "start": "2026-10-12T09:41:00+09:00",
            "end": "2026-10-12T10:11:00+09:00",
        },
    ]
