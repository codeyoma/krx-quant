"""The verdict through the engine (#676): made at each bar close once every algorithm is done, from
the barrier kinds' algorithms, stored, and served by ``GET /v1/verdict``; the trader's position read
from its store. Prices are made up."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model
from krx_quant.algorithms.contract import KindSample
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector
from krx_quant.kinds import KindForecast
from krx_quant.trader import SYMBOL
from krx_quant.trader.store import DB_NAME, TraderStore, read_held
from krx_quant.verdict import Held
from tests.algorithm_doubles import FixedAlgorithm, stand_in_card, stand_in_meanings
from tests.api_schema import check
from tests.replay_events import api, at, get_json, run, steady_book
from tests.test_trader_marks import trade


class Leaning:
    """A barrier kind's stand-in: predicts ``forecast`` (up first, timeout, down first) every time."""

    training_days = 20

    def __init__(self, name: str, kind: str, feature: str, forecast: tuple[float, float, float]) -> None:
        self.name = name
        self.kind = kind
        self.features = (feature,)
        self.card = stand_in_card(name, "a fixed barrier forecast", (feature,), "nothing")
        self.param_meanings = stand_in_meanings("samples")
        self._forecast = KindForecast(forecast)

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        return Model(f"{self.name}-fitted", {"samples": float(len(samples))})

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        return self._forecast


DOWN = (0.1, 0.2, 0.7)
MEMBERS = [
    Leaning("lean-4", "barrier", "velocity", DOWN),
    Leaning("lean-15", "barrier_15m", "velocity", DOWN),
]
DIRECTION = FixedAlgorithm(
    "steady", features=("velocity",), unique=("velocity",), forecast=Forecast(0.2, 0.5, 0.3, 0.0)
)


def morning() -> list[Any]:
    """The book at 81,200 / 81,300 every 10 s from 09:00:30 to 09:40."""
    return steady_book(at("09:00:30"), at("09:40:00"), 81_200, 81_300)


def stored(db: Path) -> dict[str, tuple[str, int]]:
    with closing(sqlite3.connect(db)) as c:
        rows = c.execute("SELECT bar_close, verdict, version FROM verdicts WHERE symbol = ?", (SYMBOL,))
        return {r[0][11:16]: (r[1], r[2]) for r in rows}


async def test_each_bar_close_has_its_verdict_from_the_barrier_algorithms_beside_the_direction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: two barrier stand-ins leaning down (0.1, 0.2, 0.7) at 4 and 15 minutes, a direction
    stand-in, nothing held. Output: each bar close from 09:01 has a stored verdict of version 1: SELL,
    avoid buying; the members with their kind, status and evidence; the 30- and 60-minute horizons
    with no member; the direction's forecast beside them, not pooled; served by ``/v1/verdict``
    within the contract; each verdict's time logged, well under a second."""
    caplog.set_level("INFO", logger="krx_quant.engine")
    db = tmp_path / "engine.db"
    engine = await run(db, morning(), until=at("09:40:00"), algorithms=[DIRECTION], kind_algorithms=MEMBERS)

    made = stored(db)
    assert {made[t] for t in ("09:01", "09:20", "09:39")} == {("sell", 1)}
    async with api(engine) as client:
        every = await get_json(client, "/v1/verdict")
        one = await get_json(client, f"/v1/verdict?symbol={SYMBOL}&bar_close=2026-10-12T09:31:00%2B09:00")
    check("verdict", every)
    check("verdict", one)
    latest, at_0931 = every["verdicts"][0], one["verdicts"][0]
    assert latest["bar_close"] == "2026-10-12T09:40:00+09:00" and at_0931["bar_close"].startswith(
        "2026-10-12T09:31"
    )
    assert (at_0931["verdict"], at_0931["meaning"], at_0931["strength"]) == ("sell", "avoid", "strong")
    members = {m["name"]: m for m in at_0931["members"]}
    assert set(members) == {"lean-4", "lean-15"}
    assert members["lean-15"]["kind"] == "barrier_15m" and members["lean-15"]["lean"] == "down"
    # Counted at the day's first verdict, before any of the day's windows was answered.
    assert members["lean-4"]["evidence"] == 0
    four = engine.store.kind_evidence("barrier", 2, ["lean-4", "lean-15", "steady"])
    fifteen = engine.store.kind_evidence("barrier_15m", 2, ["lean-15"])
    assert set(four) == {"lean-4"} and four["lean-4"] > fifteen["lean-15"] > 0, "4-minute windows end sooner"
    horizons = {h["minutes"]: h for h in at_0931["horizons"]}
    assert set(horizons) == {4, 15, 30, 60}
    assert horizons[30]["probabilities"] is None and horizons[4]["width_ticks"] == 2
    assert [s["name"] for s in at_0931["side"]] == ["steady"]
    assert at_0931["side"][0]["output"]["p_flat"] == 0.5
    timed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("verdicts at")]
    assert len(timed) == len(made)
    assert all(float(m.split(" in ")[1].split(" ms")[0]) < 200 for m in timed), timed[:3]
    engine.store.close()


async def test_held_the_verdict_reads_exit_and_without_members_wait(tmp_path: Path) -> None:
    """Input: the same morning, the trader holding a position whose time barrier is 09:40, then an
    engine with no barrier algorithm. Output: SELL meaning exit at 09:31 (the 15 minutes the horizon
    left of nine), deadline 09:40; without members WAIT ``no_members``."""
    position = Held(81_300, 81_450, 81_050, at("09:40:00"))

    def held(now: datetime) -> Mapping[str, Held]:
        return {SYMBOL: position} if now >= at("09:30:00") else {}

    db = tmp_path / "engine.db"
    engine = await run(
        db, morning(), until=at("09:35:00"), algorithms=[DIRECTION], kind_algorithms=MEMBERS, held=held
    )
    alone = await run(
        tmp_path / "alone.db", morning(), until=at("09:35:00"), algorithms=[DIRECTION], kind_algorithms=[]
    )

    async with api(engine) as client:
        at_0931 = (
            await get_json(client, f"/v1/verdict?symbol={SYMBOL}&bar_close=2026-10-12T09:31:00%2B09:00")
        )["verdicts"][0]
    async with api(alone) as client:
        nobody = (await get_json(client, "/v1/verdict"))["verdicts"][0]
    assert (at_0931["verdict"], at_0931["meaning"], at_0931["held"]) == ("sell", "exit", True)
    assert at_0931["horizon"] == {"kind": "barrier_15m", "minutes": 15}
    assert at_0931["deadline"] == "2026-10-12T09:40:00+09:00"
    assert stored(db)["09:29"] == ("sell", 1)
    assert (nobody["verdict"], nobody["reason"]) == ("wait", "no_members")
    engine.store.close()
    alone.store.close()


def test_the_trader_s_position_is_its_champion_trade_of_the_mode_it_runs_entered_and_not_exited(
    tmp_path: Path,
) -> None:
    """Input: the trader's store running paper: a paper trade bought at 10:00 and sold at 10:02, one
    bought at 10:10 still open, a mock one open and a challenger's paper one open. Output: at 10:01 the
    first is held, at 10:05 none, at 10:11 the open paper one (its target, stop and time barrier);
    before the snapshot, or without the store, nothing."""
    day = datetime.fromisoformat("2026-10-14T00:00:00+09:00")
    path = tmp_path / DB_NAME
    store = TraderStore(path)
    assert read_held(path, day) == {}
    first, second = day + timedelta(hours=10), day + timedelta(hours=10, minutes=10)
    trade(store, "paper", first, first + timedelta(minutes=2), 1)
    trade(store, "paper", second, None, 2)
    trade(store, "mock", second, None, 3)
    trade(store, "paper", second, None, 4, policy_id=None, champion=False)
    assert read_held(path, second) == {}, "no snapshot: no mode known"
    store.put_snapshot(second, {"mode": "paper"})

    assert set(read_held(path, first + timedelta(minutes=1))) == {SYMBOL}
    assert read_held(path, first + timedelta(minutes=5)) == {}
    assert read_held(path, second + timedelta(minutes=1)) == {
        SYMBOL: Held(70_100, 70_250, 69_850, second + timedelta(minutes=4))
    }
    assert read_held(tmp_path / "none.sqlite3", second) == {}
    store.close()
