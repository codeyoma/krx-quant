"""The engine with the touch target (``KRX_TARGET_VERSION=3``, #169, #187), through the Replay
Harness: the baselines and the shared normal forecast follow it.

Input: hand-made integrated book updates and a fake clock. Output: the predictions stored
and served (``/v1/symbols/{symbol}/predictions``, ``/v1/algorithms``).

The book sits at 81,200 / 81,300 (mid 81,250, k = 2, targets 81,450 and 81,050) every 10 s,
except one update at 09:32:30 with the bid at 81,500: the windows of the bar closes 09:29 to
09:32 are up, the others flat (as in ``test_touch_replay``).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model, Sample
from krx_quant.algorithms.forecasts import normal_forecast
from krx_quant.baselines import Forecast
from krx_quant.climate import CLIMATE, fit_climate
from krx_quant.climate import direction as climate_direction
from krx_quant.engine import Event
from krx_quant.features import FeatureVector
from krx_quant.feeds import KST
from krx_quant.pricing import Direction
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.api_schema import check
from tests.replay_events import SYMBOL, api, at, book, get_json, run, steady_book


def quiet_book(start: str, end: str) -> list[Event]:
    return steady_book(at(start), at(end), 81_200, 81_300, every=10)


async def served(db: Path, events: list[Event], until: str, algorithm: str, **options: Any) -> dict[str, Any]:
    """``algorithm``'s predictions by bar close (``"09:31"``) after replaying ``events``."""
    engine = await run(db, events, until=at(until), **options)
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?algorithm={algorithm}&limit=1000")
    return {p["bar_close"][11:16]: p for p in body["predictions"]}


def forecast(p: dict[str, Any]) -> tuple[Any, ...]:
    return (p["status"], p["p_up"], p["p_flat"], p["p_down"])


async def test_the_last_four_minutes_direction_is_the_touch_label_of_the_window_just_over(
    tmp_path: Path,
) -> None:
    events = quiet_book("09:00:30", "09:45:00") + [book(at("09:32:30"), 81_500, 81_600)]

    persist = await served(
        tmp_path / "engine.db", events, "09:45:00", "baseline-persist", target_version=TARGET_VERSION
    )

    # At 09:33 the window of 09:29 is over: up. At 09:37 that of 09:33: flat.
    assert forecast(persist["09:33"]) == ("ok", 1, 0, 0)
    assert forecast(persist["09:36"]) == ("ok", 1, 0, 0)  # 09:32's
    assert forecast(persist["09:37"]) == ("ok", 0, 1, 0)
    assert {p["model_version"] for p in persist.values()} == {"v2"}
    # The mid-point moved nothing over 09:29–09:33: no ticks expected, though it went up on the way.
    assert persist["09:33"]["expected_ticks"] == 0
    # 09:01–09:04: no window was followed four minutes back (before the first bar close).
    for close in ("09:01", "09:02", "09:03", "09:04"):
        assert forecast(persist[close]) == ("no_previous", None, None, None)
        assert persist[close]["outcome"] is None, "not predicted: not scored"
    assert persist["09:05"]["status"] == "ok"


async def test_the_last_four_minutes_direction_is_not_predicted_after_a_window_it_cannot_score(
    tmp_path: Path,
) -> None:
    # Nothing received from 09:35:00 to 09:36:30: the windows of 09:32 to 09:35 are silent.
    events = quiet_book("09:00:30", "09:35:00") + quiet_book("09:36:30", "09:50:00")

    persist = await served(
        tmp_path / "engine.db", events, "09:50:00", "baseline-persist", target_version=TARGET_VERSION
    )

    assert [persist[c]["status"] for c in ("09:35", "09:36", "09:39", "09:40")] == [
        "ok",  # 09:31's window ended at 09:35:00, before the gap
        "no_previous",
        "no_previous",
        "ok",  # 09:36's window opened with the book back at 09:36:30
    ]


async def test_the_last_four_minutes_direction_starts_over_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    events = quiet_book("09:00:30", "09:45:00")
    restart = at("09:33:30")

    await run(
        db, [e for e in events if e[1]["received_at"] < restart], until=restart, target_version=TARGET_VERSION
    )
    later = [e for e in events if e[1]["received_at"] >= restart]
    persist = await served(
        db, later, "09:45:00", "baseline-persist", start=restart, target_version=TARGET_VERSION
    )

    # The windows of 09:30–09:33 were not followed through: 09:34–09:37 cannot carry them on.
    assert [persist[c]["status"] for c in ("09:33", "09:34", "09:37", "09:38")] == [
        "ok",
        "no_previous",
        "no_previous",
        "ok",
    ]


async def test_the_random_baseline_draws_in_the_proportions_of_the_touch_labels_so_far(
    tmp_path: Path,
) -> None:
    # A book that never moves: every window is flat.
    events = quiet_book("09:00:30", "10:30:00")

    draws = await served(
        tmp_path / "engine.db", events, "10:30:00", "baseline-random", target_version=TARGET_VERSION
    )

    late = [p for close, p in draws.items() if close >= "10:01"]
    assert len(late) == 30
    assert {p["model_version"] for p in draws.values()} == {"v2"}
    for p in late:
        assert sorted(forecast(p)[1:]) == [0, 0, 1]  # one class, with certainty
    # By 10:00 fifty-six windows are over (09:01–09:56), all flat: flat about 57 times in 59.
    assert sum(p["p_flat"] for p in late) >= 27


async def test_the_random_baseline_counts_the_windows_over_by_its_bar_close(tmp_path: Path) -> None:
    draws = await served(
        tmp_path / "engine.db",
        quiet_book("09:00:30", "09:20:00"),
        "09:20:00",
        "baseline-random",
        target_version=TARGET_VERSION,
    )

    for minute in range(1, 16):
        close = at(f"09:{minute:02d}:00")
        over = max(0, minute - 4)  # the windows of 09:01 to four minutes back, all flat
        # Seeded by the symbol and bar close; each class counted once more (up, flat, down).
        rng = random.Random(f"baseline-random:{SYMBOL}:{close.isoformat()}")
        drawn = rng.choices(list(Direction), [1, over + 1, 1])[0]
        p = draws[f"09:{minute:02d}"]
        assert {"up": p["p_up"], "flat": p["p_flat"], "down": p["p_down"]}[drawn.value] == 1


def table(samples: Sequence[tuple[str, Direction]]) -> Model:
    """The climate table of hand-made labels at bar closes (``"09:05"``)."""
    vector = FeatureVector({}, ())
    return fit_climate([Sample(SYMBOL, at(f"{t}:00"), vector, d, None) for t, d in samples])


def test_the_climate_table_holds_each_slots_most_frequent_label_ties_to_flat_then_up() -> None:
    model = table(
        [
            ("09:05", Direction.UP),
            ("09:29", Direction.UP),
            ("09:30", Direction.FLAT),  # 09:30 closes the 09:00 slot
            ("09:31", Direction.DOWN),  # 09:30 slot: down, flat, up — a three-way tie: flat
            ("09:45", Direction.FLAT),
            ("10:00", Direction.UP),
            ("10:01", Direction.UP),  # 10:00 slot: up and down tie: up
            ("10:05", Direction.DOWN),
        ]
    )
    # And the symbol's whole day, the back-off: up 4, flat 2, down 2.
    assert dict(model.params) == {
        "005930.09:00": 1.0,
        "005930.09:30": 0.0,
        "005930.10:00": 1.0,
        "005930.all": 1.0,
    }
    assert model.version.startswith(f"{CLIMATE}-20261012-")
    assert model.trained_until is not None and model.trained is not None


def test_a_slot_the_climate_table_lacks_backs_off_to_its_symbol_s_day_then_flat() -> None:
    model = table([("09:05", Direction.DOWN), ("09:10", Direction.DOWN), ("09:31", Direction.FLAT)])
    at_open, later = datetime(2026, 10, 13, 9, 5, tzinfo=KST), datetime(2026, 10, 13, 14, 5, tzinfo=KST)
    assert climate_direction(model, "005930", at_open) is Direction.DOWN  # its slot
    assert climate_direction(model, "005930", later) is Direction.DOWN  # no 14:00 slot: the day's
    assert climate_direction(model, "000660", later) is None  # nothing known: the baseline calls flat


async def test_the_climate_baseline_calls_its_tables_direction_for_the_slot(tmp_path: Path) -> None:
    model = table([("09:05", Direction.UP), ("09:31", Direction.DOWN)])

    climate = await served(
        tmp_path / "engine.db",
        quiet_book("09:00:30", "10:10:00"),
        "10:10:00",
        CLIMATE,
        target_version=TARGET_VERSION,
        climate=model,
    )

    assert forecast(climate["09:01"]) == ("ok", 1, 0, 0)
    assert forecast(climate["09:30"]) == ("ok", 1, 0, 0)
    assert forecast(climate["09:31"]) == ("ok", 0, 0, 1)
    # A slot the table does not hold: the symbol's day (up and down once each, a tie: up).
    assert forecast(climate["10:01"]) == ("ok", 1, 0, 0)
    assert {p["model_version"] for p in climate.values()} == {model.version}


async def test_without_a_table_the_climate_baseline_calls_flat(tmp_path: Path) -> None:
    climate = await served(
        tmp_path / "engine.db",
        quiet_book("09:00:30", "09:10:00"),
        "09:10:00",
        CLIMATE,
        target_version=TARGET_VERSION,
    )
    assert {forecast(p) for p in climate.values()} == {("ok", 0, 1, 0)}
    assert {p["model_version"] for p in climate.values()} == {f"{CLIMATE}-default"}


class Reacher:
    """No direction, two ticks of spread: the shared normal forecast as it stands."""

    name = "reacher"
    features = ("flat_ticks", "spread_ticks")
    training_days = 20
    card = stand_in_card("reacher", "the shared normal forecast", ("spread_ticks",), "nothing")
    param_meanings = stand_in_meanings()

    def default_model(self) -> Model:
        return Model("reacher-default", {})

    def fit(self, samples: Sequence[Sample]) -> Model:
        return self.default_model()

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        k = features["flat_ticks"]
        assert k is not None
        return normal_forecast(0.0, 2.0, k)


@pytest.mark.parametrize(
    ("version", "spread", "p_up"),
    [
        (1, 100, 0.158655),  # 1 − Φ(2/2): ending two ticks up
        (2, 100, 0.211300),  # 2·(1 − Φ(2.5/2)): reaching two ticks and half the one-tick spread
        (2, 300, 0.080118),  # 2·(1 − Φ(3.5/2)): k is 2 at 81,350 too, half the spread 1.5
    ],
)
async def test_the_algorithms_forecast_the_engines_target(
    tmp_path: Path, version: int, spread: int, p_up: float
) -> None:
    events = steady_book(at("09:00:30"), at("09:10:00"), 81_200, 81_200 + spread, every=10)

    made = await served(
        tmp_path / "engine.db", events, "09:10:00", "reacher", target_version=version, algorithms=[Reacher()]
    )

    assert made["09:05"]["p_up"] == pytest.approx(p_up, abs=1e-6)


async def test_the_api_serves_the_touch_targets_baselines_within_its_contract(tmp_path: Path) -> None:
    engine = await run(
        tmp_path / "engine.db",
        quiet_book("09:00:30", "09:10:00"),
        until=at("09:10:00"),
        target_version=TARGET_VERSION,
    )
    async with api(engine) as client:
        listed = await get_json(client, "/v1/algorithms")
        stats = await get_json(client, f"/v1/algorithms/{CLIMATE}/stats")
        board = await get_json(client, "/v1/leaderboard")
        made = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=20")
    for definition, body in (
        ("algorithms", listed),
        ("algorithm_stats", stats),
        ("leaderboard", board),
        ("predictions", made),
    ):
        check(definition, body)
    assert [b["name"] for b in listed["baselines"]] == [
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
        CLIMATE,
    ]
    assert stats["algorithm"] == CLIMATE
    assert [b["algorithm"] for b in board["baselines"]] == [
        "baseline-climate",
        "baseline-flat",
        "baseline-persist",
        "baseline-random",
    ]
    # The first four minutes' persistence, not predicted, counted apart.
    assert board["baselines"][2]["excluded"]["status"] == {"no_previous": 4}
    assert {p["status"] for p in made["predictions"] if p["algorithm"] == "baseline-persist"} == {
        "ok",
        "no_previous",
    }
