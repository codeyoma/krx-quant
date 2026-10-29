"""The nightly re-fit of the trader's policy (#399): its values fitted on the stored predictions and
answers by hand, the rules it simulates the trades by, the same policy from the same input, no
limit in it, and its memory over 20 server-sized days.

Input: an engine database written through the prediction store (predictions, their answers and
paths, as the engine keeps them), a cost map as the Engine API gives it (``tests/trader_doubles.py``).
Output: the candidates read, the simulated trades, the policy kept in the trader's store and the
job's report. Every price and probability is made up.

The trades worked by hand throughout: the 4-minute barrier at a reference of 70,050 won, tick 100,
w 5 (target 70,550, stop 69,550), bought at the bar close's best ask 70,100, the round trip 2.0
ticks (the path's ``cost``): 14 shares of 1,000,000 won. Up first: sold at the target, 4.5 − 2.0 =
+2.5 ticks, +3,500 won. Down first: sold at the best bid then, 69,500: −6 − 2 = −8 ticks, −11,200
won. Timed out: sold at the last best bid, 70,200: +1 − 2 = −1 tick, −1,400 won.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import tracemalloc
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.baselines import Forecast
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import NewKindPrediction, NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from krx_quant.touch import TARGET_VERSION, Reached, TouchResult
from krx_quant.trader import CAPITAL, SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, open_engine_db
from krx_quant.trader.refit import (
    FEW_TRADES,
    NO_GAIN,
    NO_POLICY_KEPT,
    Candidate,
    Grid,
    Point,
    read_cell,
    refit_from,
    report,
    run_refit,
    simulate,
)
from krx_quant.trader.refit import fit as fit_cells
from krx_quant.trader.selection import Pick, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.test_trader_run import EngineApi
from tests.trader_doubles import CALENDAR, DAY, FEE, at, cost_map, cost_row, pick, predictor

BREIMAN = Cell("barrier", "breiman")
FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
CONSENSUS = Cell("direction", FINAL)
ONE = Grid(min_trades=1)  # the hand examples have a few trades


def ln(x: float) -> float:
    return math.log(x)


# Writing the engine's database as the engine does.


def moment(day: tuple[int, int, int], hms: str) -> datetime:
    """``HH:MM`` or ``HH:MM:SS`` of ``day``."""
    parts = [int(x) for x in hms.split(":")] + [0]
    return datetime(*day, parts[0], parts[1], parts[2], tzinfo=KST)


def barrier(
    store: PredictionStore,
    close: str,
    p: tuple[float, float, float],
    label: str | None,
    *,
    reached: str | None = None,
    ask: float = 70_100,
    bid_end: float = 70_200,
    cell: Cell = BREIMAN,
    version: str = "v1",
    special: bool = False,
    horizon: int = 4,
    day: tuple[int, int, int] = DAY,
) -> None:
    """A barrier prediction of ``close`` (``HH:MM``) and its answer: ``label``, the barrier first
    ``reached`` at that time (``HH:MM:SS``); w 5 around 70,050."""
    t = moment(day, close)
    anchor, tick, w = 70_050.0, 100, 5
    output = dict(zip(("p_up_first", "p_timeout", "p_down_first"), p, strict=True))
    store.add_kind_predictions(
        cell.kind,
        [
            NewKindPrediction(
                SYMBOL, cell.predictor, version, t, t + timedelta(seconds=2), output, "ok", 2, w, anchor, tick
            )
        ],
    )
    when = None if reached is None else moment(day, reached)
    path = {
        "w": w,
        "k": 2,
        "tick": tick,
        "anchor": anchor,
        "upper": anchor + w * tick,
        "lower": anchor - w * tick,
        "cost": 2.0,
        "bid_start": ask - tick,
        "ask_start": ask,
        "up_at": when.isoformat() if label == UP_FIRST and when else None,
        "up_bid": anchor + w * tick if label == UP_FIRST else None,
        "down_at": when.isoformat() if label == DOWN_FIRST and when else None,
        "down_bid": 69_500.0 if label == DOWN_FIRST else None,
        "bid_end": bid_end,
        "ask_end": bid_end + tick,
    }
    store.add_kind_answers(
        cell.kind,
        BARRIER_VERSION,
        [(SYMBOL, t, Answer(label, None if label else "ambiguous", path), special)],
        horizon=timedelta(minutes=horizon),
    )


def direction(
    store: PredictionStore,
    close: str,
    p: tuple[float, float, float],
    *,
    upper_at: str | None = None,
    ask: float = 70_100,
    bid_end: float = 70_000,
    blocked: str | None = None,
) -> None:
    """A final consensus of ``close`` and its touch label: k 5 around 70,050, the upper target
    70,550 reached at ``upper_at`` if given."""
    t = moment(DAY, close)
    mid, k = 70_050.0, 5
    added = store.add_all(
        [
            NewPrediction(
                SYMBOL,
                FINAL,
                "v1",
                t,
                t + timedelta(seconds=2),
                Forecast(*p, expected_ticks=0.0),
                t + timedelta(minutes=4),
                mid,
                mid,
                k,
                blocked=blocked,
            )
        ]
    )
    [pid] = added.ids[FINAL]
    store.score_all([(pid, Score(mid, Direction.FLAT, Direction.FLAT, None))], t + timedelta(minutes=4))
    reached = None if upper_at is None else Reached(at(upper_at), mid + k * 100, 5.0)
    store.add_touches(
        [
            TouchResult(
                SYMBOL,
                t,
                Direction.UP if reached else Direction.FLAT,
                None,
                anchor=mid,
                tick=100,
                k=k,
                upper=mid + k * 100,
                lower=mid - k * 100,
                bid_start=ask - 100,
                ask_start=ask,
                bid_end=bid_end,
                ask_end=bid_end + 100,
                upper_reached=reached,
            )
        ]
    )


def engine(state: Path) -> PredictionStore:
    store = PredictionStore(state / ENGINE_DB)
    store.record_models([("breiman", "v1", BARRIER_VERSION), ("breiman", "v0", 1)])
    return store


def candidates_of(state: Path, chosen: Pick, days: Sequence[date] = (date(*DAY),)) -> Any:
    db = open_engine_db(state / ENGINE_DB)
    try:
        windows = policies_windows(db)
        return read_cell(db, chosen, days, CALENDAR.session, windows, FEE, TARGET_VERSION)
    finally:
        db.close()


def policies_windows(db: sqlite3.Connection) -> list[Window]:
    from krx_quant.trader.cells import special_windows

    return special_windows(db, SYMBOL, datetime(*DAY, tzinfo=KST))


# The five predictions the policy is fitted on by hand: two rises right above 0.7, the others mixed.
FIVE = [
    ("10:00", (0.75, 0.15, 0.10), UP_FIRST, "10:02:00"),  # +3,500
    ("10:10", (0.55, 0.25, 0.20), DOWN_FIRST, "10:11:00"),  # −11,200
    ("10:20", (0.65, 0.20, 0.15), DOWN_FIRST, "10:21:00"),  # −11,200
    ("10:30", (0.45, 0.30, 0.25), UP_FIRST, "10:32:00"),  # +3,500
    ("10:40", (0.85, 0.10, 0.05), UP_FIRST, "10:41:00"),  # +3,500
]


def write_five(state: Path) -> None:
    store = engine(state)
    try:
        for close, p, label, reached in FIVE:
            barrier(store, close, p, label, reached=reached)
    finally:
        store.close()


PASSING = cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])
DAYS = {**PASSING, "rows": [{**r, "days": [date(*DAY).isoformat()]} for r in PASSING["rows"]]}


# The values fitted by hand.


def test_the_threshold_fitted_on_the_stored_predictions_is_the_hand_worked_one(tmp_path: Path) -> None:
    """θ 0.4 trades all five: 3 × 3,500 − 2 × 11,200 = −11,900 won; θ 0.5 (four, but the 10:30 rise)
    −15,400; θ 0.6 (10:00, 10:20, 10:40) −4,200; θ 0.7 (10:00, 10:40) +7,000; θ 0.8 (10:40) +3,500.
    θ 0.7 grows the capital most: 2 ln(1.0035). Its f* (W 10, C 2.6, lower bound 0.70 at 70,050:
    0.70 ÷ 0.017987 − 0.30 ÷ 0.010564 = 10.52) makes every λ of the grid buy the whole capital: the
    growth is the same, and λ is the default, 0.5."""
    write_five(tmp_path)

    done = refit_from(
        DAYS,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        CALENDAR.session,
        capital=CAPITAL,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        grid=ONE,
        now=at("22:31:00"),
    )

    [cell] = done.cells
    assert (cell.kind, cell.role, cell.predictor) == ("barrier", "top", "breiman")
    assert (cell.threshold, cell.kelly_fraction) == (0.7, 0.5)
    assert cell.fitted["kelly"] == pytest.approx(10.52, abs=0.01)
    assert cell.fitted["fraction"] == 1.0
    assert (cell.fitted["trades"], cell.fitted["wins"], cell.fitted["net"]) == (2, 2, pytest.approx(7_000))
    assert cell.fitted["growth"] == pytest.approx(2 * ln(1.0035))
    assert cell.fitted["read"] == {"read": 5, "rises": 5}
    assert done.fit["result"] == {
        "trades": 2,
        "wins": 2,
        "net": pytest.approx(7_000),
        "growth": pytest.approx(2 * ln(1.0035)),
        "days_traded": 1,
    }
    assert done.left_out == ()
    assert (done.version, done.champion, done.champion_version) == (1, True, 1), "the first: the champion"


def test_the_kelly_fraction_is_fitted_where_f_star_leaves_it_a_choice() -> None:
    """f* = 1: λ 0.25 buys 0.25 of the capital (3 shares at 70,100), 0.5 half (7), 1 all of it
    (14). Two trades of +2.5 ticks: 3 × 250 = +750 won each at 0.25, +1,750 at 0.5, +3,500 at 1. All
    of it grows the capital most (2 ln(1.0035) against 2 ln(1.00175) and 2 ln(1.00075))."""
    small = replace(pick(BREIMAN), kelly=1.0)
    trades = [rise("10:00", 0.75, 2.5, "10:02"), rise("10:40", 0.85, 2.5, "10:41")]

    fitted = fit_cells([small], {BREIMAN: trades}, CAPITAL, ONE)

    [kept] = fitted.kept
    assert (kept.threshold, kept.kelly_fraction, kept.fraction) == (0.6, 1.0, 1.0), (
        "θ: every point at or under 0.7 trades both: the default, 0.6"
    )
    assert fitted.result.total.net == pytest.approx(7_000)
    for fraction, net in ((0.25, 750), (0.5, 1_750), (1.0, 3_500)):
        alone = simulate([Point(small, 0.6, fraction)], {BREIMAN: trades[:1]}, CAPITAL)
        assert alone.total.net == pytest.approx(net), fraction


def rise(
    close: str, p: float, profit: float, ends: str, *, ask: float = 70_100, bought: bool = True
) -> Candidate:
    return Candidate(at(f"{close}:00"), p, ask, bought, profit, 100.0, at(f"{ends}:00"))


def test_ties_go_to_the_default_then_the_higher_threshold_then_the_lower_fraction() -> None:
    points = Grid().points()
    assert points[:4] == [(0.6, 0.5), (0.6, 0.25), (0.6, 1.0), (0.7, 0.5)]
    assert points[-1] == (0.4, 1.0)
    assert len(points) == 15, "five thresholds by three Kelly fractions"
    assert Grid(threshold=0.65).points()[:2] == [(0.7, 0.5), (0.7, 0.25)], "the .env's θ, then the higher"


def test_a_cell_is_left_out_with_too_few_trades_or_no_gain() -> None:
    """With the default grid a cell needs 20 trades at a point; one that loses at every point is
    left out though it trades enough."""
    few = [rise(f"10:{m:02d}", 0.9, 2.5, f"10:{m + 1:02d}") for m in range(0, 50, 5)]  # 10 trades
    losing = [rise(f"{h}:{m:02d}", 0.9, -1.0, f"{h}:{m + 1:02d}") for h in (10, 11) for m in range(0, 60, 5)]

    fitted = fit_cells(
        [pick(BREIMAN), pick(FIFTEEN, role="baseline")], {BREIMAN: few, FIFTEEN: losing}, CAPITAL, Grid()
    )

    assert fitted.kept == ()
    first, second = fitted.left_out
    assert (first["predictor"], first["why"], first["most_trades"]) == ("breiman", FEW_TRADES, 10)
    assert (second["predictor"], second["why"]) == (FIFTEEN.predictor, NO_GAIN)
    assert second["best"]["trades"] == 24 and second["best"]["net"] == pytest.approx(-24 * 1_400)
    assert fitted.result.total.trades == 0


def test_one_position_at_a_time_and_the_first_ranked_rise_of_a_bar_close_buys() -> None:
    """The first cell's 10:00 trade is held to 10:04: the second's 10:02 rise is held off. At 10:10
    both rise: the first ranked buys. At 10:20 the first cell's ask has reached its target: it buys
    nothing, and the second, outranked, nothing either. The second's 10:30 rise buys alone."""
    first, second = pick(BREIMAN), pick(FIFTEEN, role="baseline")
    candidates = {
        BREIMAN: [
            rise("10:00", 0.8, 2.5, "10:04"),
            rise("10:10", 0.8, 2.5, "10:12"),
            rise("10:20", 0.8, 2.5, "10:22", bought=False),
        ],
        FIFTEEN: [
            rise("10:02", 0.8, 1.0, "10:05"),
            rise("10:10", 0.8, 1.0, "10:12"),
            rise("10:20", 0.8, 1.0, "10:22"),
            rise("10:30", 0.8, 1.0, "10:31"),
        ],
    }

    result = simulate([Point(first, 0.6, 0.5), Point(second, 0.6, 0.5)], candidates, CAPITAL)

    assert (result.cells[BREIMAN].trades, result.cells[FIFTEEN].trades) == (2, 1)
    assert result.total.net == pytest.approx(2 * 3_500 + 1_400)
    assert result.days == {date(*DAY): pytest.approx(8_400)}


# The rules the trades are simulated by: the trader's.


def test_the_rises_read_are_those_the_trader_would_buy(tmp_path: Path) -> None:
    store = engine(tmp_path)
    up = (0.7, 0.2, 0.1)
    try:
        barrier(store, "09:02", up, UP_FIRST, reached="09:03:00")  # before 09:05
        barrier(store, "10:00", up, UP_FIRST, reached="10:02:00")
        barrier(store, "10:05", (0.2, 0.3, 0.5), DOWN_FIRST, reached="10:06:00")  # not a rise
        barrier(store, "10:10", up, UP_FIRST, reached="10:11:00", ask=70_600)  # the target reached already
        barrier(
            store, "10:15", up, UP_FIRST, reached="10:16:00", special=True
        )  # left out, as the cost map does
        barrier(store, "10:20", up, UP_FIRST, reached="10:21:00", version="v0")  # an older target's model
        barrier(store, "10:25", up, UP_FIRST, reached="10:26:00")  # in a VI
        barrier(store, "10:30", up, TIMEOUT)  # sold at the last best bid at the time barrier
        barrier(store, "15:12", up, UP_FIRST, reached="15:13:00")  # its barrier after 15:15
        store.keep_special(Window(SpecialKind.VI, SYMBOL, at("10:27:00"), at("10:29:00")), at("10:27:00"))
    finally:
        store.close()

    read = candidates_of(tmp_path, pick(BREIMAN))

    assert dict(read.counts) == {
        "read": 8,
        "off_target": 1,
        "before_entry": 1,
        "not_up": 1,
        "special": 1,
        "past_last_exit": 1,
        "rises": 3,
    }
    ten, target_reached, timeout = read.candidates
    assert (ten.bar_close, ten.probability, ten.ask, ten.bought) == (at("10:00:00"), 0.7, 70_100, True)
    assert (ten.profit, ten.exit_at) == (pytest.approx(2.5), at("10:02:00"))
    assert (target_reached.bought, target_reached.ask) == (False, 70_600)
    assert (timeout.profit, timeout.exit_at) == (pytest.approx(-1.0), at("10:34:00"))


def test_a_baseline_s_predictions_are_all_read_whatever_their_model(tmp_path: Path) -> None:
    store = engine(tmp_path)
    try:
        barrier(store, "10:20", (0.7, 0.2, 0.1), UP_FIRST, reached="10:21:00", cell=FIFTEEN, horizon=15)
    finally:
        store.close()

    read = candidates_of(tmp_path, pick(FIFTEEN, role="baseline"))

    assert dict(read.counts) == {"read": 1, "rises": 1}
    [candidate] = read.candidates
    assert candidate.exit_at == at("10:21:00")


def test_the_direction_s_trades_are_its_touch_p_and_l(tmp_path: Path) -> None:
    """k 5 at 70,050: the target 70,550. Bought at 70,100 and sold there: 450 won less the round
    trip on the reference, 70,050 × (0.20% + 2 × 0.015%) = 161.115 won: +2.88885 ticks. Not reached:
    sold at the last best bid, 70,000: (−100 − 161.115) ÷ 100 = −2.61115 ticks, at the end of the
    four minutes. A final consensus the engine blocked is not bought."""
    store = PredictionStore(tmp_path / ENGINE_DB)
    try:
        direction(store, "10:00", (0.7, 0.2, 0.1), upper_at="10:01:30")
        direction(store, "10:10", (0.7, 0.2, 0.1))
        direction(store, "10:20", (0.7, 0.2, 0.1), blocked="no_reference")
    finally:
        store.close()

    read = candidates_of(tmp_path, pick(CONSENSUS, role="consensus"))

    assert dict(read.counts) == {"read": 3, "blocked": 1, "rises": 2}
    reached, timed_out = read.candidates
    assert (reached.profit, reached.exit_at) == (pytest.approx(2.88885), at("10:01:30"))
    assert (timed_out.profit, timed_out.exit_at) == (pytest.approx(-2.61115), at("10:14:00"))


# The same input, the same policy; champion, then challengers.


def test_the_same_input_makes_the_same_policy_and_the_later_ones_are_challengers(tmp_path: Path) -> None:
    write_five(tmp_path)

    def again(night: str) -> Any:
        return refit_from(
            DAYS,
            tmp_path / ENGINE_DB,
            tmp_path / TRADER_DB,
            CALENDAR.session,
            capital=CAPITAL,
            fee_rate=FEE,
            target_version=TARGET_VERSION,
            grid=ONE,
            now=at(night),
        )

    first, second = again("22:31:00"), again("22:45:00")

    assert (first.version, first.champion, second.version, second.champion) == (1, True, 2, False)
    assert second.champion_version == 1, "the first stays the champion"
    assert second.cells == first.cells and second.left_out == first.left_out
    assert {**second.fit, "run": None} == {**first.fit, "run": None}, "but its time and memory"
    with closing(sqlite3.connect(tmp_path / TRADER_DB)) as db:
        cells = [r[0] for r in db.execute("SELECT cells FROM policies ORDER BY id")]
        champions = db.execute("SELECT policy_id, reason FROM champions").fetchall()
    assert cells[0] == cells[1], "byte for byte"
    assert champions == [(1, policies.FIRST)]


def test_no_cell_passing_makes_an_empty_policy(tmp_path: Path) -> None:
    PredictionStore(tmp_path / ENGINE_DB).close()
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])

    done = refit_from(
        failing,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        CALENDAR.session,
        capital=CAPITAL,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        grid=ONE,
        now=at("22:31:00"),
    )

    assert (done.cells, done.left_out, done.passed, done.champion) == ((), (), 0, True)
    assert "매매할 칸: 없음 (비용 지도를 통과한 칸이 없다)" in report(done)


# No limit in a policy; the fit changes none.


def test_a_policy_holds_no_limit_and_the_fit_changes_none(tmp_path: Path) -> None:
    write_five(tmp_path)
    narrow = TraderSettings(capital=500_000)

    done = refit_from(
        DAYS,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        CALENDAR.session,
        capital=narrow.capital,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        grid=ONE,
        now=at("22:31:00"),
    )

    stored = policies.read_policies(tmp_path / TRADER_DB, 10)["policies"][0]
    words = json.dumps(stored).lower()
    assert not re.search(r"capital|limit|loss|한도|상한", words), "no limit, nor the capital, in a policy"
    assert set(stored["cells"][0]) == {"kind", "role", "predictor", "threshold", "kelly_fraction", "fitted"}
    assert narrow == TraderSettings(capital=500_000), "the trader's settings as they were"
    # 500,000 won buys 7 shares at 70,100: the 2 trades of +2.5 ticks make 2 × 1,750 won.
    assert done.cells[0].fitted["net"] == pytest.approx(3_500)


# The job.


async def test_the_job_reads_tonight_s_cost_map_and_keeps_the_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_five(tmp_path)
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    api = EngineApi([DAYS])

    async with api.serving() as url:
        code = await run_refit(settings, TraderSettings(engine_url=url), at("22:31:00"))

    assert code == 0
    assert api.asked == [{"symbol": SYMBOL, "period": "20d", "special": "exclude"}]
    out = capsys.readouterr().out
    assert out.startswith("정책 맞추기 (#399): 정책 1 · 챔피언(처음 맞춘 정책)")
    assert "뺀 칸: barrier breiman (top): few_trades, 모의 매매가 20건에 못 미침(가장 많을 때 5건)" in out
    assert "격자: θ 0.4 0.5 0.6 0.7 0.8 × λ 0.25 0.5 1 · 칸마다 모의 매매 20건 이상" in out
    assert re.search(r"메모리: 최대 RSS \d+\.\d MB", out), out
    assert not re.search(r"70[,.]?[0-9]{3}", out), "no price in the output"
    [kept] = policies.read_policies(tmp_path / TRADER_DB, 10)["policies"]
    assert kept["cells"] == [] and kept["left_out"][0]["why"] == FEW_TRADES, (
        "the server's grid wants 20 trades a cell: five predictions are not enough"
    )


async def test_without_the_cost_map_the_job_fails_and_keeps_no_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    api = EngineApi([503])

    async with api.serving() as url:
        code = await run_refit(settings, TraderSettings(engine_url=url), at("22:31:00"))

    assert code == NO_POLICY_KEPT
    assert "비용 지도를 읽지 못해 정책을 만들지 않았다" in capsys.readouterr().out
    assert not (tmp_path / TRADER_DB).exists()


# Memory: 20 trading days of the server's size.

# Twenty weekdays to 2026-10-14 (the calendar guesses they trade), a bar close a minute from 09:01
# to 15:19: 379 a day, as the server's.
TWENTY = [d for d in (date(2026, 9, 16) + timedelta(days=i) for i in range(29)) if d.weekday() < 5][-20:]
CLOSES = [time(9 + (m // 60), m % 60) for m in range(1, 6 * 60 + 20)]
SERVER_CELLS = (
    Cell("direction", FINAL),
    Cell("barrier", "breiman"),
    Cell("barrier_15m", "barrier-persist-15m"),
    Cell("barrier_30m", "breiman-30m"),
)


def server_sized(state: Path) -> dict[str, Any]:
    """Twenty days of the four cells' predictions and answers in the engine's database, written in
    bulk; and a cost map passing them over those days."""
    store = PredictionStore(state / ENGINE_DB)
    store.record_models([("breiman", "v1", BARRIER_VERSION), ("breiman-30m", "v1", BARRIER_VERSION)])
    store.close()
    draw = random.Random(399)
    kinds, answers, predictions, outcomes, touches = [], [], [], [], []
    pid = 0
    for day in TWENTY:
        for close in CLOSES:
            t = datetime.combine(day, close, KST)
            up = draw.uniform(0.3, 0.9)
            p = (up, (1 - up) * 0.6, (1 - up) * 0.4)
            label = draw.choice((UP_FIRST, DOWN_FIRST, TIMEOUT))
            path = {
                "w": 5,
                "k": 2,
                "tick": 100,
                "anchor": 70_050.0,
                "upper": 70_550.0,
                "lower": 69_550.0,
                "cost": 2.0,
                "bid_start": 70_000.0,
                "ask_start": 70_100.0,
                "up_at": (t + timedelta(minutes=1)).isoformat() if label == UP_FIRST else None,
                "up_bid": 70_550.0 if label == UP_FIRST else None,
                "down_at": (t + timedelta(minutes=1)).isoformat() if label == DOWN_FIRST else None,
                "down_bid": 69_500.0 if label == DOWN_FIRST else None,
                "bid_end": 70_200.0,
                "ask_end": 70_300.0,
                "quotes": 300,
                "silent": False,
            }
            for cell in SERVER_CELLS[1:]:
                output = json.dumps(dict(zip(("p_up_first", "p_timeout", "p_down_first"), p, strict=True)))
                kinds.append(
                    (
                        cell.kind,
                        SYMBOL,
                        cell.predictor,
                        "v1",
                        t.isoformat(),
                        t.isoformat(),
                        "ok",
                        output,
                        2,
                        5,
                    )
                )
                answers.append((cell.kind, SYMBOL, t.isoformat(), BARRIER_VERSION, label, json.dumps(path)))
            pid += 1
            predictions.append((pid, SYMBOL, FINAL, "v1", t.isoformat(), t.isoformat(), *p))
            outcomes.append((pid, (t + timedelta(minutes=4)).isoformat(), 70_050.0, 5, t.isoformat()))
            touch = {**path, "upper_at": path["up_at"], "lower_at": path["down_at"]}
            touches.append(
                (
                    SYMBOL,
                    t.isoformat(),
                    TARGET_VERSION,
                    "up" if label == UP_FIRST else "flat",
                    json.dumps(touch),
                )
            )
    with closing(sqlite3.connect(state / ENGINE_DB)) as db, db:
        db.executemany(
            "INSERT INTO kind_predictions (kind, symbol, algorithm, model_version, bar_close, displayed_at,"
            " status, output, flat_ticks, width_ticks, anchor, tick) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " 70050.0, 100)",
            kinds,
        )
        db.executemany(
            "INSERT INTO kind_answers (kind, symbol, bar_close, answer_version, class, unscorable, special,"
            " path, scored_at) VALUES (?, ?, ?, ?, ?, NULL, 0, ?, '2026-10-14T16:00:00+09:00')",
            answers,
        )
        db.executemany(
            "INSERT INTO predictions (id, symbol, algorithm, model_version, bar_close, displayed_at, p_up,"
            " p_flat, p_down, expected_ticks, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'ok')",
            predictions,
        )
        db.executemany(
            "INSERT INTO outcomes (prediction_id, target_at, mid_close, flat_ticks, special, scored_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            outcomes,
        )
        db.executemany(
            "INSERT INTO touches (symbol, bar_close, target_version, class, unscorable, path, scored_at)"
            " VALUES (?, ?, ?, ?, NULL, ?, '2026-10-14T16:00:00+09:00')",
            touches,
        )
    days = [d.isoformat() for d in TWENTY]
    rows = [
        cost_row("direction", consensus=predictor(FINAL, 0.70, "pass", rank=None)),
        cost_row("barrier", top=predictor("breiman", 0.70, "pass")),
        cost_row("barrier_15m", baseline=predictor("barrier-persist-15m", 0.70, "pass")),
        cost_row("barrier_30m", top=predictor("breiman-30m", 0.70, "pass")),
    ]
    return cost_map([{**r, "days": days} for r in rows])


# Tracing every allocation slows the fit several times over (about 15 s here, 2.5 s untraced).
@pytest.mark.timeout(120)
def test_twenty_server_sized_days_are_fitted_in_little_memory(tmp_path: Path) -> None:
    """Twenty days of 379 bar closes, four cells: 30,320 predictions read, a cell at a time. The
    Python memory the fit allocates at its peak, its candidates included, stays under 24 MB (11 MB
    on a Mac). The process's resident peak is the job's own figure
    (``test_the_job_prints_its_peak_resident_memory``)."""
    body = server_sized(tmp_path)
    assert len(choose(body).picks) == 4

    tracemalloc.start()
    try:
        done = refit_from(
            body,
            tmp_path / ENGINE_DB,
            tmp_path / TRADER_DB,
            CALENDAR.session,
            capital=CAPITAL,
            fee_rate=FEE,
            target_version=TARGET_VERSION,
            grid=Grid(),
            now=datetime.combine(TWENTY[-1], time(22, 31), KST),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    read = sum(c.fitted["read"]["read"] for c in done.cells) + sum(x["read"]["read"] for x in done.left_out)
    assert read == 4 * 20 * len(CLOSES)
    print(f"refit over 20 server-sized days: {peak / 1024 / 1024:.1f} MB traced at the peak")
    assert peak < 24 * 1024 * 1024, f"{peak / 1024 / 1024:.1f} MB"


@pytest.mark.timeout(120)  # a process of its own: the CLI's imports, then the 20 days (a few seconds each)
async def test_the_job_prints_its_peak_resident_memory(tmp_path: Path) -> None:
    """``krx-quant refit-policy`` as the server runs it, in a process of its own, over twenty
    server-sized days: its output carries its peak resident memory, well under the server's 2 GB
    (about 140 MB here, the CLI's imports most of it)."""
    state = tmp_path / "state"
    state.mkdir()
    body = server_sized(state)
    api = EngineApi([body])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "KRX_STATE_DIR": str(state),
        "KRX_DATA_DIR": str(tmp_path / "data"),
        "KRX_FEE_PERCENT": "0.015",
    }
    async with api.serving() as url:
        ran = await _run(
            [sys.executable, "-m", "krx_quant.cli", "refit-policy", "--engine-url", url], tmp_path, env
        )

    assert ran.returncode == 0, ran.stderr
    found = re.search(r"메모리: 최대 RSS (\d+\.\d) MB", ran.stdout)
    assert found, ran.stdout
    peak = float(found.group(1))
    print(f"refit-policy over 20 server-sized days: peak RSS {peak} MB")
    assert 0 < peak < 512


async def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    assert process.returncode is not None
    return subprocess.CompletedProcess(args, process.returncode, out.decode(), err.decode())
