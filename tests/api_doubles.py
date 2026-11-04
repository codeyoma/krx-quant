"""Scored predictions and Engine API reads for the tests (#511): sure calls of the direction scored
against made-up outcomes with their touch labels, the baselines', the barrier's board, the cost map
served on a replayed holiday, a GET checked for its status, and the stream read until an event.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import ClientWebSocketResponse
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, PERSIST, TIMEOUT, UP_FIRST
from krx_quant.baselines import Forecast
from krx_quant.daily import StoredDays
from krx_quant.engine import PredictionEngine
from krx_quant.kinds import Answer
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import NewKindPrediction, NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.touch import Reached, TouchResult
from tests.api_schema import check
from tests.replay_events import DAY, at, run

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
# What happened at the 40 bar closes from 09:01: up, flat, down, flat, ...
OUTCOMES = [UP, FLAT, DOWN, FLAT] * 10


def touch(symbol: str, t: datetime, label: Direction | None, unscorable: str | None = None) -> TouchResult:
    """Bar close ``t``'s touch label: at 81,250 won between 81,200 and 81,300 (k = 2), the
    upper target 81,450 reached a minute in for up, the lower 81,050 for down, neither for
    flat; the quotes back where they started at the end. None: ``unscorable``."""
    if label is None:
        return TouchResult(symbol, t, None, unscorable or "silent quotes")
    reached = Reached(t + timedelta(minutes=1), 81_450 if label is UP else 81_050, None)
    return TouchResult(
        symbol,
        t,
        label,
        None,
        anchor=81_250,
        tick=100,
        k=2,
        upper=81_450,
        lower=81_050,
        bid_start=81_200,
        ask_start=81_300,
        bid_end=81_200,
        ask_end=81_300,
        upper_reached=reached if label is UP else None,
        lower_reached=reached if label is DOWN else None,
    )


def store_scored(
    store: PredictionStore,
    algorithm: str,
    calls: Sequence[Direction],
    *,
    symbol: str = "005930",
    day: date = DAY,
    version: str = "v1",
    special: bool = False,
) -> None:
    """Store ``calls`` (one sure call per bar close from 09:01), scored against OUTCOMES: the
    bar closes' touch labels (and, unread, the old bases' classes)."""
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
        label = OUTCOMES[i]
        target = 81_250 + {UP: 300, FLAT: 0, DOWN: -300}[label]
        store.score_all(
            [(pending.prediction_id, Score(target, label, label, None))], t + timedelta(minutes=4)
        )
        store.add_touches([touch(symbol, t, label)])


async def get(client: TestClient[Any, Any], path: str, status: int = 200) -> Any:
    response = await client.get(path)
    assert response.status == status, await response.text()
    return await response.json()


def baselines(store: PredictionStore, n: int = 40, **kw: Any) -> None:
    store_scored(store, "baseline-flat", [FLAT] * n, **kw)
    store_scored(store, "baseline-persist", [UP] * n, **kw)
    store_scored(store, "baseline-random", [DOWN] * n, **kw)


SYMBOLS = ("005930", "000660", "009150")


# The barrier's answers at the 40 bar closes from 09:01, as the direction's OUTCOMES.
BARRIER_OUTCOMES = [UP_FIRST, TIMEOUT, DOWN_FIRST, TIMEOUT] * 10


@contextlib.asynccontextmanager
async def cost_map(
    tmp_path: Path, min_sample: int = 20
) -> AsyncIterator[tuple[PredictionEngine, TestClient[Any, Any]]]:
    # A holiday: no mid-point now, so a card's price is the latest bar close's.
    engine = await run(
        tmp_path / "engine.db",
        [],
        until=at("16:00:00"),
        calendar=MarketCalendar({DAY: False}),
        symbols=SYMBOLS,
        target_version=1,
        daily=StoredDays(tmp_path / "data"),  # the daily kind's row
    )
    app = create_app(engine, min_sample=min_sample, state_dir=tmp_path / "state")
    async with TestClient(TestServer(app)) as client:
        yield engine, client


def store_barrier(store: PredictionStore, algorithm: str, calls: Sequence[str], width: int = 10) -> None:
    """``calls`` (one sure call per bar close from 09:01) of the barrier on 005930, answered by
    BARRIER_OUTCOMES, ``width`` ticks from 81,250 won."""
    start = datetime.combine(DAY, at("09:01:00").timetz())
    made = []
    for i, call in enumerate(calls):
        t = start + timedelta(minutes=i)
        output = dict(zip(BARRIER.p_keys(), (float(c == call) for c in BARRIER.classes), strict=True))
        made.append(NewKindPrediction("005930", algorithm, "v1", t, t, output, width_ticks=width))
    store.add_kind_predictions(BARRIER.name, made)
    store.record_models([(algorithm, "v1", BARRIER_VERSION)])
    path = {"w": width, "anchor": 81_250, "tick": 100, "cost": 1.86875, "upper": 82_250, "lower": 80_250}
    path |= {"bid_start": 81_200, "ask_start": 81_300, "bid_end": 81_200, "ask_end": 81_300}
    store.add_kind_answers(
        BARRIER.name,
        BARRIER_VERSION,
        [
            (s, start + timedelta(minutes=i), Answer(label, None, path), False)
            for s in ("005930",)
            for i, label in enumerate(BARRIER_OUTCOMES[: len(calls)])
        ],
    )


def barrier_board(store: PredictionStore) -> None:
    store_barrier(store, CLIMATE, [TIMEOUT] * 40)
    store_barrier(store, PERSIST, [UP_FIRST] * 40)
    store_barrier(store, "alpha", BARRIER_OUTCOMES)  # always right


async def receive_until(
    ws: ClientWebSocketResponse, done: Callable[[dict[str, Any]], bool], timeout: float = 5.0
) -> list[dict[str, Any]]:
    """Events until one is ``done``, each checked against the schema."""
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while True:
            event = await ws.receive_json()
            check("event", event)
            events.append(event)
            if done(event):
                return events
