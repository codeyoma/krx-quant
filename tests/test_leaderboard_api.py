"""The leaderboard and algorithm stats in the Engine API.

Input: scored predictions stored through the prediction store, with their bar closes'
touch labels (#170: what every prediction is scored on). Output: the API's leaderboard
and per-algorithm stats.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
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
from krx_quant.touch import TARGET_VERSION, Reached, TouchResult
from tests.api_schema import check
from tests.replay_events import DAY, at, run
from tests.test_replay import busy_morning

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
# What happened at the 40 bar closes from 09:01: up, flat, down, flat, ...
OUTCOMES = [UP, FLAT, DOWN, FLAT] * 10
EARLIER = date(2026, 10, 8)


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


@contextlib.asynccontextmanager
async def board(
    tmp_path: Path, min_sample: int = 20, target_version: int = 1
) -> AsyncIterator[tuple[PredictionEngine, TestClient[Any, Any]]]:
    # A holiday: the engine predicts nothing itself; the test stores what it needs.
    engine = await run(
        tmp_path / "engine.db",
        [],
        until=at("16:00:00"),
        calendar=MarketCalendar({DAY: False}),
        symbols=("005930", "000660"),
        target_version=target_version,
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

    assert (body["symbol"], body["period"], body["min_sample"]) == ("all", "today", 20)
    assert "basis" not in body, "gone with #170: one label, the touch label"
    assert body["days"] == ["2026-10-12"]
    assert [(e["rank"], e["algorithm"]) for e in body["ranked"]] == [(1, "alpha"), (2, "beta")]
    alpha = body["ranked"][0]
    assert alpha["n"] == 40
    assert alpha["accuracy"]["value"] == 1.0
    # The touch P&L (#170) of its 20 up and down calls, each right: in at 81,300 (out at 81,200),
    # out at the target 81,450 (back at 81,050): 1.5 ticks less 1.86875 of cost each.
    assert alpha["pnl"]["trades"] == 20
    assert alpha["pnl"]["total_ticks"] == pytest.approx(20 * (1.5 - 1.86875))
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


async def test_the_leaderboard_chooses_symbol_period_and_leaves_out_special(tmp_path: Path) -> None:
    async with board(tmp_path, min_sample=1) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES)
        store_scored(engine.store, "alpha", OUTCOMES[:8], symbol="000660")
        store_scored(engine.store, "alpha", OUTCOMES[:4], day=EARLIER)
        store_scored(engine.store, "delta", OUTCOMES[:4], special=True)

        everything = await get(client, "/v1/leaderboard")
        one_symbol = await get(client, "/v1/leaderboard?symbol=005930")
        five_days = await get(client, "/v1/leaderboard?symbol=005930&period=5d")
        with_special = await get(client, "/v1/leaderboard?special=include")

    def alpha(body: Any) -> Any:
        return next(e for e in body["ranked"] + body["unranked"] if e["algorithm"] == "alpha")

    assert alpha(everything)["n"] == 48  # both symbols, today
    assert alpha(one_symbol)["n"] == 40 and alpha(one_symbol)["accuracy"]["value"] == 1.0
    assert five_days["days"] == ["2026-10-12", "2026-10-08"]
    assert alpha(five_days)["n"] == 44
    assert all(e["algorithm"] != "delta" for e in everything["ranked"] + everything["unranked"])
    assert any(e["algorithm"] == "delta" for e in with_special["ranked"] + with_special["unranked"])


async def test_on_the_touch_target_the_stats_count_only_models_trained_on_it(tmp_path: Path) -> None:
    """Input: alpha's predictions by an older model (the four-minute move's), always wrong on
    005930, and by a model trained on the touch target, always right on 000660. Output: the
    leaderboard and alpha's stats count the second only, as the statuses do (#170)."""
    async with board(tmp_path, target_version=TARGET_VERSION) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", [DOWN] * 40, version="alpha-old")
        store_scored(engine.store, "alpha", OUTCOMES, version="alpha-new", symbol="000660")
        engine.store.record_models([("alpha", "alpha-old", 1), ("alpha", "alpha-new", TARGET_VERSION)])

        body = await get(client, "/v1/leaderboard")
        stats = await get(client, "/v1/algorithms/alpha/stats")

    alpha = next(e for e in body["ranked"] + body["unranked"] if e["algorithm"] == "alpha")
    assert (alpha["n"], alpha["accuracy"]["value"]) == (40, 1.0)
    assert list(stats["by_model_version"]) == ["alpha-new"]
    assert {b["algorithm"] for b in body["baselines"]} >= {"baseline-flat", "baseline-climate"}


async def test_on_version_3_neither_version_2_s_models_nor_its_labels_count(tmp_path: Path) -> None:
    """Input (#187): alpha's predictions on 005930 by a model of the touch target's version 2
    (crossed quotes dropped), right throughout; on 000660 by a model of version 3, right on
    version 3's labels; and every 000660 bar close's version 2 label too, always down (an
    engine before #187 wrote them). Output: the leaderboard counts the version 3 model's 40,
    scored on version 3's labels: all right."""
    async with board(tmp_path, target_version=TARGET_VERSION) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES, version="alpha-v2")
        store_scored(engine.store, "alpha", OUTCOMES, version="alpha-v3", symbol="000660")
        engine.store.record_models([("alpha", "alpha-v2", 2), ("alpha", "alpha-v3", TARGET_VERSION)])
        with contextlib.closing(sqlite3.connect(tmp_path / "engine.db")) as conn, conn:
            conn.execute(
                "INSERT INTO touches (symbol, bar_close, target_version, class, unscorable, path, scored_at)"
                " SELECT symbol, bar_close, 2, 'down', NULL, path, scored_at FROM touches"
                " WHERE symbol = '000660'"
            )

        body = await get(client, "/v1/leaderboard?symbol=000660")
        everywhere = await get(client, "/v1/leaderboard")

    for board_body in (body, everywhere):
        alpha = next(e for e in board_body["ranked"] + board_body["unranked"] if e["algorithm"] == "alpha")
        assert (alpha["n"], alpha["accuracy"]["value"]) == (40, 1.0)


async def test_a_screen_from_before_still_sends_a_basis_and_reads_it_back(tmp_path: Path) -> None:
    """Input: a leaderboard and an algorithm's stats asked with basis=a or basis=b, as the
    screen before #170 asks (it asks again until the answer's basis is its own). Output: the
    same stats as without it, on the touch label, with ``basis`` echoed as sent, after
    ``days``; any other basis is refused."""
    async with board(tmp_path, min_sample=1) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES)

        plain = await get(client, "/v1/leaderboard")
        on_a = await get(client, "/v1/leaderboard?basis=a")
        on_b = await get(client, "/v1/leaderboard?basis=b")
        stats_plain = await get(client, "/v1/algorithms/alpha/stats")
        stats_a = await get(client, "/v1/algorithms/alpha/stats?basis=a")
        refused = await get(client, "/v1/leaderboard?basis=touch", status=400)

    for body, basis in ((on_a, "a"), (on_b, "b")):
        check("leaderboard", body)
        assert body["basis"] == basis
        assert list(body)[: list(body).index("basis") + 1][-2:] == ["days", "basis"]
        assert {k: v for k, v in body.items() if k != "basis"} == plain
    check("algorithm_stats", stats_a)
    assert stats_a["basis"] == "a" and "basis" not in stats_plain
    assert {k: v for k, v in stats_a.items() if k != "basis"} == stats_plain
    assert "error" in refused


# Alpha against always-flat, day by day: right throughout (a gain of 0.5), right at the
# first 20 bar closes and flat after (0.25), flat throughout (0). Over the three days:
# a mean of 0.25 and a standard deviation of 0.25.
DAILY_CALLS = [OUTCOMES, OUTCOMES[:20] + [FLAT] * 20, [FLAT] * 40]
THREE_DAYS = [EARLIER, date(2026, 10, 9), DAY]
T_975_2 = 4.302652729911275  # Student's t, 97.5th percentile, 2 degrees of freedom (tables)


async def test_each_lift_is_also_judged_by_trading_day(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        for day, calls in zip(THREE_DAYS, DAILY_CALLS, strict=True):
            baselines(engine.store, day=day)
            store_scored(engine.store, "alpha", calls, day=day)

        three_days = await get(client, "/v1/leaderboard?period=5d")
        today = await get(client, "/v1/leaderboard")
        stats = await get(client, "/v1/algorithms/alpha/stats?period=5d")

    check("leaderboard", three_days)
    check("algorithm_stats", stats)

    def alpha(body: Any) -> Any:
        return next(e for e in body["ranked"] + body["unranked"] if e["algorithm"] == "alpha")

    lift = alpha(three_days)["lift"]
    by_day = lift["vs"]["baseline-flat"]["by_day"]
    error = 0.25 / math.sqrt(3)
    assert (by_day["n"], by_day["value"]) == (3, pytest.approx(0.25))
    assert (by_day["low"], by_day["high"]) == (
        pytest.approx(0.25 - T_975_2 * error),
        pytest.approx(0.25 + T_975_2 * error),
    )
    # With 2 degrees of freedom the t distribution has a closed form: P(T > x) = 1/2 - x / (2√(2 + x²)).
    x = 0.25 / error
    assert by_day["p"] == pytest.approx(0.5 - x / (2 * math.sqrt(2 + x * x)))
    assert lift["day_low"] == min(v["by_day"]["low"] for v in lift["vs"].values())
    assert stats["overall"]["lift"]["vs"]["baseline-flat"]["by_day"] == by_day
    # One day says nothing of how the days differ.
    assert alpha(today)["lift"]["vs"]["baseline-flat"]["by_day"] is None
    assert alpha(today)["lift"]["day_low"] is None


async def test_an_algorithms_stats_break_down_by_symbol_slot_and_model_version(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        store_scored(engine.store, "alpha", OUTCOMES[:30], version="alpha-1")
        store_scored(engine.store, "alpha", OUTCOMES[:30], symbol="000660", version="alpha-2")

        body = await get(client, "/v1/algorithms/alpha/stats")

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


# Scored, but its bar close has no touch label: before the recording, not filled in yet.
NO_LABEL = "no label"


def store_one(
    store: PredictionStore,
    algorithm: str,
    t: datetime,
    *,
    forecast: Forecast | None = SURE[UP],
    status: str = "ok",
    label: Direction | str | None = UP,
    special: bool = False,
    symbol: str = "005930",
) -> None:
    """One prediction at bar close ``t``, scored: its touch label ``label``, a label it could
    not be scored on (``None``, silent quotes), no label row at all (``NO_LABEL``), or, with
    ``label`` ``"pending"``, not yet over."""
    added = store.add_all(
        [
            NewPrediction(
                symbol,
                algorithm,
                "v1",
                t,
                t,
                forecast,
                t + timedelta(minutes=4),
                81_250,
                81_250,
                2,
                status,
                special,
            )
        ]
    )
    if label == "pending" or not added.pending:
        return
    store.score_all([(added.pending[0].prediction_id, Score(81_550, UP, UP, None))], t + timedelta(minutes=4))
    if label != NO_LABEL:
        store.add_touches([touch(symbol, t, Direction(label) if label else None)])


async def test_calibration_is_also_shown_class_by_class(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        # Every class given 1/3, ten times, and the price went up every time (after the
        # baselines' bar closes: a bar close has one touch label, every predictor's).
        for i in range(10):
            store_one(
                engine.store,
                "even",
                at("10:01:00") + timedelta(minutes=i),
                forecast=Forecast(1 / 3, 1 / 3, 1 / 3, 0),
            )

        body = await get(client, "/v1/algorithms/even/stats")

    check("algorithm_stats", body)
    overall = body["overall"]
    # Pooled, the three classes look perfectly calibrated: 1/3 forecast, 1/3 happened.
    pooled = overall["calibration"][3]
    assert (pooled["count"], pooled["observed"]["value"]) == (30, pytest.approx(1 / 3))
    # Class by class they do not: up always happened, flat and down never did.
    by_class = overall["calibration_by_class"]
    assert [len(by_class[c]) for c in ("up", "flat", "down")] == [10, 10, 10]
    observed = {c: (by_class[c][3]["count"], by_class[c][3]["observed"]["value"]) for c in by_class}
    assert observed == {"up": (10, 1.0), "flat": (10, 0.0), "down": (10, 0.0)}
    assert by_class["up"][3]["mean_p"] == pytest.approx(1 / 3)


async def test_the_stats_count_what_they_leave_out(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        baselines(engine.store)
        # After the baselines' bar closes: a bar close has one touch label, every predictor's.
        t = [at("10:01:00") + timedelta(minutes=i) for i in range(12)]
        store = engine.store
        for i in range(3):
            store_one(store, "alpha", t[i])  # scored on its touch label
        for i in range(3, 5):
            store_one(store, "alpha", t[i], special=True)  # a VI or breaker touched it
        store_one(store, "alpha", t[5], label=None)  # its bar close could not be labelled
        store_one(store, "alpha", t[6], label=NO_LABEL)  # its bar close has no label at all
        store_one(store, "alpha", t[7], label="pending")  # still pending: neither
        for i, status in zip(range(8, 12), ("vi", "vi", "timed_out", "no_book"), strict=True):
            store_one(store, "alpha", t[i], forecast=None, status=status)
        store_one(store, "alpha", t[2] + timedelta(seconds=1), special=True, label=None)  # both: unscorable
        store_one(store, "alpha", at("09:01:00", EARLIER), status="error", forecast=None)  # another day
        store_one(store, "alpha", t[0], forecast=None, status="vi", symbol="000660")  # another symbol
        store_one(store, "alpha", t[1], symbol="000660", label=None)

        one = await get(client, "/v1/leaderboard?symbol=005930")
        every = await get(client, "/v1/leaderboard?special=include")
        stats = await get(client, "/v1/algorithms/alpha/stats?symbol=005930")

    check("leaderboard", one)
    check("leaderboard", every)
    check("algorithm_stats", stats)
    [alpha_one] = [e for e in one["unranked"] if e["algorithm"] == "alpha"]
    assert alpha_one["n"] == 3
    # The one both special and unscorable counts once, as unscorable.
    assert alpha_one["excluded"] == {
        "special": 2,
        "unscorable": 3,
        "status": {"no_book": 1, "timed_out": 1, "vi": 2},
    }
    # Every symbol: the other symbol's VI and unscorable too; with special included, the two
    # are counted in.
    [alpha_every] = [e for e in every["unranked"] if e["algorithm"] == "alpha"]
    assert alpha_every["n"] == 5
    assert alpha_every["excluded"] == {
        "special": 2,
        "unscorable": 4,
        "status": {"no_book": 1, "timed_out": 1, "vi": 3},
    }
    assert stats["overall"]["excluded"] == alpha_one["excluded"]
    # A baseline that left nothing out says so.
    assert one["baselines"][0]["excluded"] == {"special": 0, "unscorable": 0, "status": {}}


async def test_bad_choices_and_unknown_algorithms_are_refused(tmp_path: Path) -> None:
    async with board(tmp_path) as (engine, client):
        store_scored(engine.store, "retired", OUTCOMES[:4], day=EARLIER)
        for query in ("period=week", "basis=c", "symbol=999999", "special=maybe"):  # basis: a or b only
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
        body = await get(client, "/v1/leaderboard")
        newton = await get(client, "/v1/algorithms/newton/stats")

    # 09:01–09:56 are scored by 10:00 (09:57–10:00 wait for their targets).
    assert {(e["algorithm"], e["n"]) for e in body["ranked"]} == {
        ("newton", 56),
        ("gauss", 56),
        ("granger", 56),
        ("markov", 56),
        ("kalman", 56),
        ("shannon", 56),
        ("poisson", 56),
        ("turing", 56),
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
