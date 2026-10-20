"""The cost map in the Engine API (#307).

Input: scored predictions of the direction, the barrier and the daily kind, stored through the
prediction store with their answers; the bar closes' mid-points (their touch paths); the period
base rates the history task keeps, from hand-made one-minute bars. Output: ``GET /v1/cost-map``,
within the /v1 schema, against the leaderboard and the stats window of the same query and
against ``structure-check``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, PERSIST, TIMEOUT, UP_FIRST
from krx_quant.base_rates import NOT_COMPUTED, keep_base_rates
from krx_quant.cost_map import NOT_SYMMETRIC
from krx_quant.daily import StoredDays
from krx_quant.engine import PredictionEngine
from krx_quant.kinds import Answer
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import ANSWER_VERSION as OTC_VERSION
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.pricing import Direction
from krx_quant.structure_check import PERIODS, structure_check
from krx_quant.touch import TouchResult
from tests.api_schema import check
from tests.replay_events import DAY, at, run
from tests.test_leaderboard_api import OUTCOMES, baselines, get, store_scored
from tests.test_structure_check import SYMBOL, bar, flat_bar, write_bars

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SYMBOLS = ("005930", "000660", "009150")
FEE = 0.00015
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


async def ask(client: TestClient[Any, Any], query: str = "") -> Any:
    body = await get(client, f"/v1/cost-map{query}")
    check("cost_map", body)
    return body


def priced(store: PredictionStore, symbol: str, mid: float, tick: int) -> datetime:
    """A flat touch label at 15:00 with ``mid`` its mid-point: the symbol's latest bar close."""
    t = datetime.combine(DAY, time(15, 0), at("09:00:00").tzinfo)
    store.add_touches(
        [
            TouchResult(
                symbol,
                t,
                FLAT,
                None,
                anchor=mid,
                tick=tick,
                k=1,
                upper=mid + tick,
                lower=mid - tick,
                bid_start=mid - tick / 2,
                ask_start=mid + tick / 2,
                bid_end=mid - tick / 2,
                ask_end=mid + tick / 2,
            )
        ]
    )
    return t


def row(body: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(r for r in body["rows"] if r["kind"] == kind)


def entry(board: dict[str, Any], algorithm: str) -> dict[str, Any]:
    return next(
        e for e in board["ranked"] + board["unranked"] + board["baselines"] if e["algorithm"] == algorithm
    )


async def test_a_symbols_card_is_its_costs_at_its_latest_mid_point(tmp_path: Path) -> None:
    """199,900 won: a 100-won tick (5.0bp) under the 200,000 edge, k 5; 210,000 won: a 500-won
    tick (23.8bp) over it, k 1. 0.015% a side: 3bp of commissions, 23bp with the sell tax."""
    async with cost_map(tmp_path) as (engine, client):
        none = await ask(client, "?symbol=009150")
        samsung = priced(engine.store, "005930", 199_900, 100)
        priced(engine.store, "000660", 210_000, 500)
        priced(engine.store, "009150", 150_000, 100)
        body = await ask(client)

    [electro] = none["symbols"]
    assert (electro["price"], electro["price_at"], electro["costs"]) == (None, None, None)
    cards = {s["symbol"]: s for s in body["symbols"]}
    assert list(cards) == list(SYMBOLS)
    first = cards["005930"]
    assert (first["price"], first["price_at"], first["live"]) == (199_900, samsung.isoformat(), False)
    c = first["costs"]
    assert (c["tick"], c["k"]) == (100, 5)
    assert c["tick_bp"] == pytest.approx(5.0025, abs=1e-4)
    assert (c["sell_tax_bp"], c["fees_bp"]) == (pytest.approx(20), pytest.approx(3))
    assert c["spread_bp"] == pytest.approx(c["tick_bp"])
    assert c["limit"] == {"bp": pytest.approx(23), "ticks": pytest.approx(199_900 * 0.0023 / 100)}
    assert c["market"]["ticks"] == pytest.approx(199_900 * 0.0023 / 100 + 1)
    assert c["market"]["bp"] == pytest.approx(23 + c["tick_bp"])
    # The −2x product: both commissions on half the notional, in the stock's basis points.
    inverse = c["inverse"]
    assert (inverse["leverage"], inverse["listed"], inverse["omits"]) == (
        2,
        "2026-05-27",
        ["spread", "premium", "basis"],
    )
    assert inverse["bp"] == pytest.approx(1.5)
    assert inverse["ticks"] == pytest.approx(199_900 * 0.00015 / 100)
    assert c["above"] == {"edge": 200_000, "price": 200_000, "tick": 500, "k": 1}
    assert c["below"] == {"edge": 50_000, "price": 49_950, "tick": 50, "k": 3}
    second = cards["000660"]["costs"]
    assert (second["tick"], second["k"]) == (500, 1)
    assert second["tick_bp"] == pytest.approx(23.8095, abs=1e-4)
    assert second["below"] == {"edge": 200_000, "price": 199_900, "tick": 100, "k": 5}
    assert second["above"] == {"edge": 500_000, "price": 500_000, "tick": 1_000, "k": 2}
    assert cards["009150"]["costs"]["inverse"] is None, "no single-stock inverse product"


async def test_the_latest_mid_point_is_found_behind_any_number_of_bar_closes_without_one(
    tmp_path: Path,
) -> None:
    """A long quote outage: 80 bar closes after 15:00 with no mid-point; the card's is still 15:00's."""
    async with cost_map(tmp_path) as (engine, client):
        priced_at = priced(engine.store, "005930", 81_250, 100)
        engine.store.add_touches(
            [
                TouchResult("005930", priced_at + timedelta(minutes=1 + i), None, "silent quotes")
                for i in range(80)
            ]
        )
        body = await ask(client, "?symbol=005930")

    [card] = body["symbols"]
    assert (card["price"], card["price_at"]) == (81_250, priced_at.isoformat())


async def test_the_direction_s_row_sets_its_hit_rates_against_its_breakeven(tmp_path: Path) -> None:
    """At 81,250 won (tick 100) k is 2 and the round trip 1.86875 ticks, 2.86875 at the market:
    p* = (2 + 1.86875) / 4 and (2 + 2.86875) / 4. Alpha calls the 20 rises and falls right."""
    async with cost_map(tmp_path) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES)
        store_scored(engine.store, "consensus-final", [FLAT if o is DOWN else o for o in OUTCOMES])
        body = await ask(client)
        board = await get(client, "/v1/leaderboard")
        stats = await get(client, "/v1/algorithms/alpha/stats")

    direction = row(body, "direction")
    assert (direction["unit"], direction["bar_closes"], direction["horizon_minutes"]) == ("ticks", 40, 4)
    assert (direction["width"], direction["reference_price"], direction["tick"]) == (2, 81_250, 100)
    assert direction["cost"] == {"limit": pytest.approx(1.86875), "market": pytest.approx(2.86875)}
    assert direction["breakeven"] == {
        "limit": {"c_over_w": pytest.approx(0.934375), "p_star": pytest.approx(0.9671875)},
        "market": {"c_over_w": pytest.approx(1.434375), "p_star": pytest.approx(1.2171875)},
    }
    assert direction["breakeven_missing"] is None
    top, consensus, baseline = direction["top"], direction["consensus"], direction["baseline"]
    assert (top["algorithm"], top["rank"], top["calls"], top["edge"]) == ("alpha", 1, 20, 1.0)
    # The leaderboard's own hit rate and P&L, and the stats window's.
    assert top["precision"] == entry(board, "alpha")["precision"] == stats["overall"]["precision"]
    assert top["pnl"]["per_trade"] == entry(board, "alpha")["pnl"]["per_trade"]
    assert top["pnl_inverse"]["trades"] == entry(board, "alpha")["pnl_inverse"]["trades"]
    # Every call right, 20 of them: the interval's lower bound (0.84) is below either p*.
    assert top["precision"]["low"] < 0.9671875
    assert top["verdict"] == {"limit": "fail", "market": "fail"}
    assert (consensus["algorithm"], consensus["calls"], consensus["edge"]) == ("consensus-final", 10, 1.0)
    assert consensus["precision"] == entry(board, "consensus-final")["precision"]
    assert consensus["verdict"] == {"limit": "few", "market": "few"}, "10 calls, fewer than 20"
    # Always up and always down hit a quarter alike, and lose as often: the first by name.
    assert (baseline["algorithm"], baseline["calls"], baseline["edge"]) == ("baseline-persist", 40, 0.0)
    assert baseline["precision"] == entry(board, "baseline-persist")["precision"]


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


async def test_a_barrier_row_passes_where_its_hit_rate_s_lower_bound_is_above_p_star(tmp_path: Path) -> None:
    """A 10-tick barrier at 81,250 won: p* = (10 + 1.86875) / 20, 0.59; alpha's 20 calls all
    right put the lower bound at 0.84."""
    async with cost_map(tmp_path) as (engine, client):
        barrier_board(engine.store)
        body = await ask(client)
        board = await get(client, "/v1/leaderboard?kind=barrier")

    barrier = row(body, "barrier")
    assert (barrier["unit"], barrier["width"], barrier["bar_closes"]) == ("ticks", 10, 40)
    assert barrier["breakeven"]["limit"]["p_star"] == pytest.approx((10 + 1.86875) / 20)
    assert barrier["breakeven"]["market"]["p_star"] == pytest.approx((10 + 2.86875) / 20)
    assert barrier["consensus"] is None, "the kinds have no consensus"
    top = barrier["top"]
    assert (top["algorithm"], top["calls"], top["edge"]) == ("alpha", 20, 1.0)
    assert top["precision"] == entry(board, "alpha")["precision"]
    assert top["pnl"]["trades"] == entry(board, "alpha")["extra"]["pnl"]["trades"] == 20
    assert top["verdict"] == {"limit": "pass", "market": "pass"}
    baseline = barrier["baseline"]
    assert (baseline["algorithm"], baseline["calls"]) == (PERSIST, 40), "always timeout calls nothing"
    assert baseline["verdict"] == {"limit": "fail", "market": "fail"}


async def test_fewer_calls_than_the_leaderboard_s_minimum_sample_are_too_few(tmp_path: Path) -> None:
    """With 30 a minimum, alpha's 40 predictions rank it, but its 20 calls are too few."""
    async with cost_map(tmp_path, min_sample=30) as (engine, client):
        barrier_board(engine.store)
        body = await ask(client)

    barrier = row(body, "barrier")
    assert barrier["top"]["algorithm"] == "alpha"
    assert barrier["top"]["verdict"] == {"limit": "few", "market": "few"}
    assert barrier["baseline"]["verdict"] == {"limit": "fail", "market": "fail"}


async def test_the_daily_kind_s_row_is_in_basis_points_without_a_breakeven(tmp_path: Path) -> None:
    """open_to_close: bands of 40, 50 and 60bp, the median 50; the round trip 23bp, the auctions
    filling every order at one price. Its band is not a barrier: no p*."""
    async with cost_map(tmp_path) as (engine, client):
        t = OPEN_TO_CLOSE.predicted_at(DAY)
        made, answered = [], []
        for symbol, band, change in zip(SYMBOLS, (0.004, 0.005, 0.006), (0.01, -0.01, 0.0), strict=True):
            path: dict[str, Any] = {
                "open": 100_000,
                "close": 100_000 * (1 + change),
                "change": change,
                "band": band,
            }
            path |= {"sigma": band * 2, "scale": 0.43, "fee_rate": FEE, "source": "krx_minute_bar"}
            label = "up" if change > band else "down" if change < -band else "flat"
            output = dict(zip(OPEN_TO_CLOSE.p_keys(), (0.5, 0.25, 0.25), strict=True))
            made.append(NewKindPrediction(symbol, "otc-climate", "v1", t, t, output, band=band))
            answered.append((symbol, t, Answer(label, None, path), False))
        engine.store.add_kind_predictions(OPEN_TO_CLOSE.name, made)
        engine.store.add_kind_answers(OPEN_TO_CLOSE.name, OTC_VERSION, answered)
        body = await ask(client)

    assert [r["kind"] for r in body["rows"]] == [
        "direction",
        "barrier",
        "barrier_15m",
        "barrier_30m",
        "barrier_60m",
        "open_to_close",
        "open_to_close_5d",
    ], "the range has no direction"
    otc = row(body, "open_to_close")
    assert (otc["unit"], otc["horizon_minutes"], otc["bar_closes"]) == ("bp", None, 3)
    assert otc["width"] == pytest.approx(50)
    assert otc["cost"] == {"limit": pytest.approx(23), "market": pytest.approx(23)}
    assert (otc["breakeven"], otc["breakeven_missing"]) == (None, NOT_SYMMETRIC)
    climate = otc["baseline"]
    # Three rise calls: one rose, one fell, one stayed.
    assert (climate["algorithm"], climate["calls"], climate["edge"]) == ("otc-climate", 3, 0.0)
    assert climate["verdict"] is None
    assert climate["pnl"]["trades"] == 3


async def test_the_period_base_rates_are_structure_check_s(tmp_path: Path) -> None:
    """The bar rule's labels of a day after the listing (``test_structure_check``'s): 376
    windows, four of them up; the history task keeps them, the cost map serves them."""
    day = date(2026, 6, 8)
    price = 81_200
    bars = [flat_bar(day, h, m, price) for h in range(9, 16) for m in range(60) if (h, m) < (15, 20)]
    bars[next(i for i, b in enumerate(bars) if b.start.time() == time(10, 0))] = bar(
        day, 10, 0, price, price + 300, price, price
    )
    data = tmp_path / "data"
    write_bars(data, day, bars)
    calendar = MarketCalendar({day: True})

    async with cost_map(tmp_path) as (_, client):
        before = await ask(client)
        keep_base_rates(data, tmp_path / "state", calendar, SYMBOLS, FEE, at("20:31:00"))
        after = await ask(client)
        other = await ask(client, "?symbol=000660")

    assert (before["regimes"], before["regimes_missing"]) == (None, NOT_COMPUTED)
    regimes = after["regimes"]
    assert after["regimes_missing"] is None
    assert (regimes["computed_at"], regimes["first_day"], regimes["last_day"]) == (
        "2026-10-12T20:31:00+09:00",
        "2026-06-08",
        "2026-06-08",
    )
    [one] = regimes["rows"]
    assert (one["symbol"], one["period"], one["from"], one["to"]) == (
        SYMBOL,
        PERIODS[1][0],
        "2026-05-27",
        "2026-07-30",
    )
    assert (one["days"], one["windows"], one["k"], one["persist_windows"]) == (1, 376, 2, 372)
    line = (
        f"{SYMBOL} {one['period']}: {one['windows']:,}창 · {100 * one['up']:.1f}/{100 * one['flat']:.1f}/"
        f"{100 * one['down']:.1f}% · k {one['k']} · 직전 창 {100 * one['persist']:.1f}% "
        f"(n={one['persist_windows']:,}) · 늘 보합 {100 * one['flat']:.1f}%"
    )
    assert line in structure_check(data, tmp_path / "state", calendar, [SYMBOL], fee_rate=FEE)
    assert other["regimes"]["rows"] == [], "000660 has no bars"
