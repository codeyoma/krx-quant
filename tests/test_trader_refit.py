"""The nightly re-fit of the trader's policy (#399): its values fitted by hand on recorded days
replayed by the paper trader's rules (#470), the same policy from the same input, no limit in it,
and its time and memory over 20 server-sized days.

Input: an engine database written through the prediction store (predictions as the engine keeps
them), the day's quotes recorded as the collector records them (``trader_doubles.record_quotes``), a
cost map as the Engine API gives it (``tests/trader_doubles.py``). Output: the policy kept in the
trader's store and the job's report. Every price and probability is made up.

The trades worked by hand throughout: the 4-minute barrier at a reference of 70,050 won, tick 100, w 5
(target 70,550, stop 69,550), displayed 2 s after its bar close and bought at the best ask of the
first quote after that, 70,100 (the bar close's standing ask 70,000 is not the buy's): 14 shares of
1,000,000 won. Up: sold at the target once a bid reaches 70,600: 450 won a share less the commission
on 70,100 and 70,550 and the tax on 70,550 (10.515 + 10.5825 + 141.1) = +287.8025 won a share,
+4,029.235 won. Down: stopped at the bid 69,500: −600 − (10.515 + 10.425 + 139) = −759.94 won a share,
−10,639.16 won.
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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds import BOOK
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import CAPITAL, SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.cells import Cell, open_engine_db, special_windows
from krx_quant.trader.paper import (
    BEFORE_ENTRY,
    NOT_UP,
    PAST_LAST_EXIT,
    SPECIAL,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_REACHED,
    net_of,
)
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.refit import (
    FEW_TRADES,
    NO_GAIN,
    NO_POLICY_KEPT,
    Grid,
    Point,
    Replay,
    refit_from,
    report,
    run_refit,
)
from krx_quant.trader.refit import fit as fit_cells
from krx_quant.trader.replay import OFF_TARGET, Replayer, Result, Source, Tally, day_reads
from krx_quant.trader.selection import Pick, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.test_trader_run import EngineApi
from tests.trader_doubles import CALENDAR, DAY, FEE, at, cost_map, cost_row, pick, predictor, record_quotes

BREIMAN = Cell("barrier", "breiman")
FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
CONSENSUS = Cell("direction", FINAL)
ONE = Grid(min_trades=1)  # the hand examples have a few trades
UP = 14 * net_of(70_100, 70_550, 1, FEE)  # a take-profit of 14 shares
DOWN = 14 * net_of(70_100, 69_500, 1, FEE)  # a stop of 14 shares


def ln(x: float) -> float:
    return math.log(x)


def test_the_hand_worked_trades() -> None:
    assert UP == pytest.approx(4_029.235) and DOWN == pytest.approx(-10_639.16)


# Writing the engine's database as the engine does, and the quotes as the collector records them.


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


def engine(state: Path) -> PredictionStore:
    store = PredictionStore(state / ENGINE_DB)
    store.record_models([("breiman", "v1", BARRIER_VERSION), ("breiman", "v0", 1)])
    return store


def quotes_of(close: str, label: str | None, day: tuple[int, int, int] = DAY) -> list[BestQuote]:
    """The quotes of a barrier prediction of ``close``: the book standing at the bar close, the first
    quote after its display (bought at 70,100), then the bid at 70,600 (up first: the take-profit),
    or at 69,500 (down first: the stop), or neither until the time barrier (sold at 70,000)."""
    t = moment(day, close)
    rows = [(t + timedelta(seconds=1), 69_900, 70_000), (t + timedelta(seconds=3), 70_000, 70_100)]
    if label == UP_FIRST:
        rows.append((t + timedelta(minutes=2), 70_600, 70_700))
    elif label == DOWN_FIRST:
        rows.append((t + timedelta(minutes=1), 69_500, 69_600))
    else:
        rows.append((t + timedelta(minutes=3, seconds=58), 70_000, 70_100))
    rows.append((t + timedelta(minutes=5), 70_000, 70_100))
    return [BestQuote(when, bid, ask, 10.0, 10.0) for when, bid, ask in rows]


# The five predictions the policy is fitted on by hand: two rises right above 0.7, the others mixed.
FIVE = [
    ("10:00", (0.75, 0.15, 0.10), UP_FIRST, "10:02:00"),  # +4,029.235
    ("10:10", (0.55, 0.25, 0.20), DOWN_FIRST, "10:11:00"),  # −10,639.16
    ("10:20", (0.65, 0.20, 0.15), DOWN_FIRST, "10:21:00"),  # −10,639.16
    ("10:30", (0.45, 0.30, 0.25), UP_FIRST, "10:32:00"),  # +4,029.235
    ("10:40", (0.85, 0.10, 0.05), UP_FIRST, "10:42:00"),  # +4,029.235
]


def write_five(state: Path, data: Path | None = None) -> None:
    """FIVE in the engine's database in ``state`` and their quotes recorded under ``data`` (by default
    ``state / "data"``)."""
    store = engine(state)
    try:
        for close, p, label, reached in FIVE:
            barrier(store, close, p, label, reached=reached)
    finally:
        store.close()
    record_quotes(data or state / "data", [q for close, _, label, _ in FIVE for q in quotes_of(close, label)])


PASSING = cost_map([cost_row("barrier", top=predictor("breiman", 0.70, "pass"))])
DAYS = {**PASSING, "rows": [{**r, "days": [date(*DAY).isoformat()]} for r in PASSING["rows"]]}


def refit(state: Path, body: dict[str, Any] = DAYS, *, store: Path | None = None, **kwargs: Any) -> Any:
    """``refit_from`` on ``state``'s engine database and recordings, by default at 22:31 of the test day
    into ``state``'s trader store, on the hand examples' grid."""
    args: dict[str, Any] = {
        "capital": CAPITAL,
        "fee_rate": FEE,
        "target_version": TARGET_VERSION,
        "grid": ONE,
        "now": at("22:31:00"),
    }
    return refit_from(
        body,
        state / ENGINE_DB,
        store or state / TRADER_DB,
        state / "data",
        CALENDAR.session,
        **(args | kwargs),
    )


def replayer_on(state: Path, picks: Sequence[Pick], days: Sequence[date] = (date(*DAY),)) -> Replay:
    """The fit's replay (``refit_from``'s) of ``picks`` on ``state``'s database and recordings."""
    db = open_engine_db(state / ENGINE_DB)
    windows = special_windows(db, SYMBOL, datetime(*DAY, tzinfo=KST))
    replayer = Replayer(
        db,
        state / "data",
        CALENDAR.session,
        windows,
        {p.cell: Source(p.role, tuple(days)) for p in picks},
        capital=CAPITAL,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        thresholds=ONE.thresholds,
    )

    def replay(chosen: Pick, policies: Sequence[Sequence[Point]]) -> Sequence[Result]:
        return replayer.run([[p.ranked() for p in policy] for policy in policies], alone=chosen.cell).results

    return replay


# The values fitted by hand.


def test_the_threshold_fitted_on_the_replayed_days_is_the_hand_worked_one(tmp_path: Path) -> None:
    """θ 0.4 trades all five: 3 × 4,029.235 − 2 × 10,639.16 = −9,190.615 won; θ 0.5 (four, but the
    10:30 rise) −13,219.85; θ 0.6 (10:00, 10:20, 10:40) −2,580.69; θ 0.7 (10:00, 10:40) +8,058.47; θ
    0.8 (10:40) +4,029.235. θ 0.7 grows the capital most: 2 ln(1.004029235). Its f* (W 10, C 2.6,
    lower bound 0.70 at 70,050: 0.70 ÷ 0.017987 − 0.30 ÷ 0.010564 = 10.52) makes every λ of the grid
    buy the whole capital: the growth is the same, and λ is the default, 0.5."""
    write_five(tmp_path)

    done = refit(tmp_path)

    [cell] = done.cells
    assert (cell.kind, cell.role, cell.predictor) == ("barrier", "top", "breiman")
    assert (cell.threshold, cell.kelly_fraction) == (0.7, 0.5)
    assert cell.fitted["kelly"] == pytest.approx(10.52, abs=0.01)
    assert cell.fitted["fraction"] == 1.0
    assert (cell.fitted["trades"], cell.fitted["wins"], cell.fitted["net"]) == (2, 2, pytest.approx(2 * UP))
    assert cell.fitted["growth"] == pytest.approx(2 * ln(1 + UP / CAPITAL))
    assert cell.fitted["read"] == {"read": 5, "rises": 5}
    assert done.fit["result"] == {
        "trades": 2,
        "wins": 2,
        "net": pytest.approx(2 * UP),
        "growth": pytest.approx(2 * ln(1 + UP / CAPITAL)),
        "days_traded": 1,
        "missing": {},
    }
    assert done.fit["replay"] == {"days": 1, "no_recording": [], "gaps": [], "passes": 1, "days_read": 1}
    assert done.left_out == ()
    assert (done.version, done.champion, done.champion_version) == (1, True, 1), "the first: the champion"
    # Each signal alone, a share, at each θ (#470): the five at 0.4, the two at 0.7.
    signals = cell.fitted["signals"]
    assert list(signals) == ["0.4", "0.5", "0.6", "0.7", "0.8"]
    assert (signals["0.4"]["signals"], signals["0.4"]["bought"], signals["0.7"]["bought"]) == (5, 5, 2)
    assert signals["0.7"]["net"] == pytest.approx(2 * UP / 14)
    assert signals["0.4"]["exits"] == {STOP_LOSS: 2, TAKE_PROFIT: 3}


def test_the_kelly_fraction_is_fitted_where_f_star_leaves_it_a_choice(tmp_path: Path) -> None:
    """f* = 1: λ 0.25 buys 0.25 of the capital (3 shares at 70,100), 0.5 half (7), 1 all of it (14). Two
    take-profits of +287.8025 won a share. All of it grows the capital most."""
    store = engine(tmp_path)
    try:
        barrier(store, "10:00", (0.75, 0.15, 0.10), UP_FIRST, reached="10:02:00")
        barrier(store, "10:40", (0.85, 0.10, 0.05), UP_FIRST, reached="10:42:00")
    finally:
        store.close()
    record_quotes(tmp_path / "data", [*quotes_of("10:00", UP_FIRST), *quotes_of("10:40", UP_FIRST)])
    small = replace(pick(BREIMAN), kelly=1.0)

    fitted = fit_cells([small], replayer_on(tmp_path, [small]), ONE)

    [kept] = fitted.kept
    assert (kept.threshold, kept.kelly_fraction, kept.fraction) == (0.6, 1.0, 1.0), (
        "θ: every point at or under 0.7 trades both: the default, 0.6"
    )
    assert fitted.result.total.net == pytest.approx(2 * UP)
    replay = replayer_on(tmp_path, [small])
    for fraction, shares in ((0.25, 3), (0.5, 7), (1.0, 14)):
        [alone] = replay(small, [[Point(small, 0.6, fraction)]])
        assert alone.total.net == pytest.approx(2 * shares * UP / 14), fraction


def test_ties_go_to_the_default_then_the_higher_threshold_then_the_lower_fraction() -> None:
    points = Grid().points()
    assert points[:4] == [(0.6, 0.5), (0.6, 0.25), (0.6, 1.0), (0.7, 0.5)]
    assert points[-1] == (0.4, 1.0)
    assert len(points) == 15, "five thresholds by three Kelly fractions"
    assert Grid(threshold=0.65).points()[:2] == [(0.7, 0.5), (0.7, 0.25)], "the .env's θ, then the higher"


def made_up(trades: dict[Cell, tuple[int, float]]) -> Replay:
    """A replay whose each cell makes ``trades[cell]``: so many trades of so many won each, at every
    point."""

    def replay(chosen: Pick, policies: Sequence[Sequence[Point]]) -> Sequence[Result]:
        out = []
        for policy in policies:
            total, cells = Tally(), {}
            for point in policy:
                n, net = trades[point.pick.cell]
                cells[point.pick.cell] = tally = Tally()
                for _ in range(n):
                    tally.add(net, CAPITAL)
                    total.add(net, CAPITAL)
            out.append(Result(total, cells, {}))
        return out

    return replay


def test_a_cell_is_left_out_with_too_few_trades_or_no_gain() -> None:
    """With the default grid a cell needs 20 trades at a point; one that loses at every point is
    left out though it trades enough."""
    replay = made_up({BREIMAN: (10, 3_500.0), FIFTEEN: (24, -1_400.0)})

    fitted = fit_cells([pick(BREIMAN), pick(FIFTEEN, role="baseline")], replay, Grid())

    assert fitted.kept == ()
    first, second = fitted.left_out
    assert (first["predictor"], first["why"], first["most_trades"]) == ("breiman", FEW_TRADES, 10)
    assert (second["predictor"], second["why"]) == (FIFTEEN.predictor, NO_GAIN)
    assert second["best"]["trades"] == 24 and second["best"]["net"] == pytest.approx(-24 * 1_400)
    assert fitted.result.total.trades == 0


def test_each_pick_is_one_pass_of_its_points_with_the_cells_kept_before() -> None:
    """The first pick's fifteen points, then the second's, each with the first as it was kept."""
    asked: list[list[list[tuple[Cell, float, float]]]] = []
    inner = made_up({BREIMAN: (30, 1_000.0), FIFTEEN: (30, 500.0)})

    def replay(chosen: Pick, policies: Sequence[Sequence[Point]]) -> Sequence[Result]:
        asked.append([[(p.pick.cell, p.threshold, p.kelly_fraction) for p in policy] for policy in policies])
        return inner(chosen, policies)

    fitted = fit_cells([pick(BREIMAN), pick(FIFTEEN, role="baseline")], replay, Grid())

    assert [len(policies) for policies in asked] == [15, 15]
    assert all(len(policy) == 1 for policy in asked[0])
    assert {policy[0] for policy in asked[1]} == {(BREIMAN, 0.6, 0.5)}, "the first as kept"
    assert [p.pick.cell for p in fitted.kept] == [BREIMAN, FIFTEEN]


# The predictions the replay reads: those the trader would buy.


def test_the_rises_read_are_those_the_trader_would_buy(tmp_path: Path) -> None:
    """Of the predictions read: one before 09:05, one not a rise, one of an older target's model, one in
    a VI known when it was read, one whose time barrier is after 15:15 are kept out; one in a VI that
    began after it was read is bought (the trader did not know of it then), as is one whose answer
    the engine marked special. The one whose first quote's ask is at its target is not bought."""
    store = engine(tmp_path)
    up = (0.7, 0.2, 0.1)
    try:
        barrier(store, "09:02", up, UP_FIRST, reached="09:03:00")  # before 09:05
        barrier(store, "10:00", up, UP_FIRST, reached="10:02:00")
        barrier(store, "10:05", (0.2, 0.3, 0.5), DOWN_FIRST, reached="10:06:00")  # not a rise
        barrier(store, "10:15", up, UP_FIRST, reached="10:16:00", special=True)  # its answer special
        barrier(store, "10:20", up, UP_FIRST, reached="10:21:00", version="v0")  # an older target's model
        barrier(store, "10:25", up, UP_FIRST, reached="10:26:00")  # a VI from 10:27, after its read
        barrier(store, "10:31", up, TIMEOUT)  # in the VI, known when read
        barrier(store, "10:40", up, UP_FIRST, reached="10:41:00")  # its target reached already
        barrier(store, "15:12", up, UP_FIRST, reached="15:13:00")  # its barrier after 15:15
        store.keep_special(Window(SpecialKind.VI, SYMBOL, at("10:27:00"), at("10:32:00")), at("10:27:00"))
    finally:
        store.close()
    quotes = [*quotes_of("10:00", UP_FIRST), *quotes_of("10:15", UP_FIRST), *quotes_of("10:25", UP_FIRST)]
    quotes += [BestQuote(at("10:40:03"), 70_500, 70_600)]
    record_quotes(tmp_path / "data", quotes)

    done = refit(tmp_path, grid=Grid(min_trades=1, thresholds=(0.6,), kelly_fractions=(0.5,)))

    [cell] = done.cells
    assert cell.fitted["read"] == {
        "read": 9,
        "rises": 4,
        BEFORE_ENTRY: 1,
        NOT_UP: 1,
        OFF_TARGET: 1,
        PAST_LAST_EXIT: 1,
        SPECIAL: 1,
    }
    alone = cell.fitted["signals"]["0.6"]
    assert (alone["signals"], alone["bought"], alone["refused"]) == (4, 3, {TARGET_REACHED: 1})
    assert cell.fitted["trades"] == 3, "10:00, 10:15 and 10:25"


def test_a_baseline_s_predictions_are_all_read_whatever_their_model(tmp_path: Path) -> None:
    store = engine(tmp_path)
    try:
        barrier(store, "10:20", (0.7, 0.2, 0.1), UP_FIRST, reached="10:21:00", cell=FIFTEEN, horizon=15)
        barrier(store, "10:30", (0.7, 0.2, 0.1), UP_FIRST, reached="10:31:00", version="v0")
    finally:
        store.close()

    db = open_engine_db(tmp_path / ENGINE_DB)
    try:
        baseline = day_reads(db, FIFTEEN, "baseline", date(*DAY), TARGET_VERSION)
        top = day_reads(db, BREIMAN, "top", date(*DAY), TARGET_VERSION)
        as_baseline = day_reads(db, BREIMAN, "baseline", date(*DAY), TARGET_VERSION)
    finally:
        db.close()

    assert [(r.signal.bar_close, r.on_target) for r in baseline] == [(at("10:20:00"), True)]
    assert [r.on_target for r in top] == [False], "an algorithm's, by a model of the old target"
    assert [r.on_target for r in as_baseline] == [True]


# The same input, the same policy; champion, then challengers.


def test_the_same_input_makes_the_same_policy_and_it_is_not_kept_twice(tmp_path: Path) -> None:
    """Fitted into two stores, byte for byte the same policy. Fitted again into the first, it has
    the champion's values: not kept (#400), the champion stays."""
    write_five(tmp_path)

    first = refit(tmp_path)
    other = refit(tmp_path, store=tmp_path / "other" / TRADER_DB, now=at("22:45:00"))
    second = refit(tmp_path, now=at("22:50:00"))

    assert (first.version, first.champion, other.version, other.champion) == (1, True, 1, True)
    assert other.cells == first.cells and other.left_out == first.left_out
    assert {**other.fit, "run": None} == {**first.fit, "run": None}, "but its time and memory"
    stored = []
    for path in (tmp_path / TRADER_DB, tmp_path / "other" / TRADER_DB):
        with closing(sqlite3.connect(path)) as db:
            stored.append([r[0] for r in db.execute("SELECT cells FROM policies ORDER BY id")])
            champions = db.execute("SELECT policy_id, reason FROM champions").fetchall()
        assert champions == [(1, policies.FIRST)]
    assert stored[0] == stored[1], "byte for byte, and one policy each"
    assert (second.version, second.same_as, second.champion, second.champion_version) == (1, 1, False, 1)
    assert report(second).startswith(
        "정책 맞추기 (#399): 새 정책 없음 · 맞춘 값이 챔피언 정책 1과 같아 저장하지 않았다\n"
    )


def test_no_cell_passing_makes_an_empty_policy(tmp_path: Path) -> None:
    PredictionStore(tmp_path / ENGINE_DB).close()
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])

    done = refit(tmp_path, failing)

    assert (done.cells, done.left_out, done.passed, done.champion) == ((), (), 0, True)
    assert done.fit["replay"]["days"] == 0
    assert "매매할 칸: 없음 (비용 지도를 통과한 칸이 없다)" in report(done)


def test_a_day_without_its_recording_trades_nothing_and_is_counted(tmp_path: Path) -> None:
    """The five predictions, no recording: every rise left out (``no_recording``), none a trade."""
    write_five(tmp_path, data=tmp_path / "elsewhere")

    done = refit(tmp_path)

    assert done.cells == () and done.left_out[0]["why"] == FEW_TRADES
    assert done.fit["replay"]["no_recording"] == [date(*DAY).isoformat()]
    assert done.left_out[0]["signals"]["0.4"]["missing"] == {"no_recording": 5}
    assert "(녹화 없음 1일, 장중 결측 구간 0일)" in report(done)


# No limit in a policy; the fit changes none.


def test_a_policy_holds_no_limit_and_the_fit_changes_none(tmp_path: Path) -> None:
    write_five(tmp_path)
    narrow = TraderSettings(capital=500_000)

    done = refit(tmp_path, capital=narrow.capital)

    stored = policies.read_policies(tmp_path / TRADER_DB, 10)["policies"][0]
    words = json.dumps(stored).lower().replace(STOP_LOSS, "exit")  # the signals' exits: no limit
    assert not re.search(r"capital|limit|loss|한도|상한", words), "no limit, nor the capital, in a policy"
    assert set(stored["cells"][0]) == {"kind", "role", "predictor", "threshold", "kelly_fraction", "fitted"}
    assert narrow == TraderSettings(capital=500_000), "the trader's settings as they were"
    # 500,000 won buys 7 shares at 70,100: the 2 take-profits make 2 × 7 × 287.8025 won.
    assert done.cells[0].fitted["net"] == pytest.approx(UP)


# The job.


async def test_the_job_reads_tonight_s_cost_map_and_keeps_the_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_five(tmp_path)
    settings = Settings(
        kis_app_key="", kis_app_secret="", state_dir=tmp_path, data_dir=tmp_path / "data", fee_rate=FEE
    )
    api = EngineApi([DAYS])

    async with api.serving() as url:
        code = await run_refit(settings, TraderSettings(engine_url=url), at("22:31:00"))

    assert code == 0
    assert api.asked == [{"symbol": SYMBOL, "period": "20d", "special": "exclude"}]
    out = capsys.readouterr().out
    assert out.startswith("도전 정책 판정 (#400)\n챔피언이 아직 없다(첫 밤 정책 맞추기 전).\n\n"), (
        "the challengers are judged first (#400): none before the first champion"
    )
    assert "\n정책 맞추기 (#399): 정책 1 · 챔피언(처음 맞춘 정책)\n" in out
    assert "뺀 칸: barrier breiman (top): few_trades, 모의 매매가 20건에 못 미침(가장 많을 때 5건)" in out
    assert "격자: θ 0.4 0.5 0.6 0.7 0.8 × λ 0.25 0.5 1 · 칸마다 모의 매매 20건 이상" in out
    assert (
        "재생 (#470): 녹화한 날을 trader의 모의 체결 규칙 그대로 다시 돌린다 · 1일(녹화 없음 0일, 장중 결측 "
        "구간 0일) · 재생 1번, 날 읽기 1번" in out
    )
    assert "   1주씩 따로 잰 신호: θ 0.4 신호 5, 체결 5, 신호당 " in out
    assert re.search(r"메모리: 최대 RSS (\d+\.\d|\?) MB", out), out
    assert not re.search(r"(69|70)[,.]?[0-9]{3}", out), "no price in the output"
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


# Time and memory: 20 trading days of the server's size.

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
# The integrated best quotes of a recorded day on the server: about 570,000 (the book's and the
# trades', ``passive.py``'s measurement).
SERVER_QUOTES = 570_000


def record_day(data: Path, day: date, count: int) -> None:
    """``count`` best quotes of 005930 over ``day``'s session, written in bulk where the collector
    writes the book, in row groups of the archive's merged file's size: the bid wanders a few ticks
    around 70,000 (two waves), the ask a tick above it."""
    start = datetime.combine(day, time(9), KST)
    seconds = np.sort(np.random.default_rng(day.toordinal()).uniform(0, 6.5 * 3600, count))
    ticks = np.round(4 * np.sin(2 * np.pi * seconds / 900) + 2 * np.sin(2 * np.pi * seconds / 97))
    bids = (70_000 + 100 * ticks).astype(np.int64)
    opened = np.datetime64(start.astimezone(UTC).replace(tzinfo=None), "us")
    received = opened + (seconds * 1e6).astype("m8[us]")
    table = pa.table(
        {
            "received_at": pa.array(received, pa.timestamp("us", tz="UTC")),
            "bid_px_1": pa.array(bids, pa.int64()),
            "ask_px_1": pa.array(bids + 100, pa.int64()),
        }
    )
    partition = data / "events" / f"date={day.isoformat()}" / f"feed={BOOK.tr_id}" / f"symbol={SYMBOL}"
    partition.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, partition / "part-000000-test.parquet", row_group_size=256_000)


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


def timed_refit(state: Path, body: dict[str, Any]) -> Any:
    return refit_from(
        body,
        state / ENGINE_DB,
        state / TRADER_DB,
        state / "data",
        CALENDAR.session,
        capital=CAPITAL,
        fee_rate=FEE,
        target_version=TARGET_VERSION,
        grid=Grid(),
        now=datetime.combine(TWENTY[-1], time(22, 31), KST),
    )


# Tracing every allocation slows the replay several times over (about 25 s here with the fixture).
@pytest.mark.timeout(300)
def test_twenty_server_sized_days_are_replayed_in_little_memory(tmp_path: Path) -> None:
    """Twenty days of 379 bar closes, four cells, 570,000 quotes a day: 30,320 predictions, each pass a
    day's recording at a time. The Python memory the fit allocates at its peak, a day's quotes
    included, stays under 64 MB (27 MB on a Mac). The process's resident peak is the job's own figure
    (``test_the_job_prints_its_peak_resident_memory``)."""
    body = server_sized(tmp_path)
    for day in TWENTY:
        record_day(tmp_path / "data", day, SERVER_QUOTES)
    assert len(choose(body).picks) == 4

    tracemalloc.start()
    try:
        done = timed_refit(tmp_path, body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    read = sum(c.fitted["read"]["read"] for c in done.cells) + sum(x["read"]["read"] for x in done.left_out)
    assert read == 4 * 20 * len(CLOSES)
    assert done.fit["replay"]["passes"] == 4 and done.fit["replay"]["days_read"] == 4 * 20
    assert set(done.fit["run"]["days"]) == {d.isoformat() for d in TWENTY}, "each day's peak"
    print(f"refit over 20 server-sized days: {peak / 1024 / 1024:.1f} MB traced at the peak")
    assert peak < 64 * 1024 * 1024, f"{peak / 1024 / 1024:.1f} MB"


@pytest.mark.timeout(120)  # a process of its own: the CLI's imports, then four passes over the 20 days
async def test_the_job_prints_its_peak_resident_memory(tmp_path: Path) -> None:
    """``krx-quant refit-policy`` as the server runs it, in a process of its own, over twenty
    server-sized days: its output carries its peak resident memory and each day's highest, well under
    the server's 2 GB."""
    state = tmp_path / "state"
    state.mkdir()
    body = server_sized(state)
    for day in TWENTY:
        record_day(tmp_path / "data", day, SERVER_QUOTES)
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
    found = re.search(r"메모리: 최대 RSS (\d+\.\d) MB\([^)]*\), 하루 최대 (\d+\.\d) MB", ran.stdout)
    assert found, ran.stdout
    peak, day_peak = float(found.group(1)), float(found.group(2))
    took = re.search(r" · (\d+\.\d)초", ran.stdout)
    seconds = took.group(1) if took else "?"
    print(f"refit-policy over 20 server-sized days: peak RSS {peak} MB, a day's {day_peak} MB, {seconds} s")
    assert 0 < day_peak <= peak < 512


async def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    assert process.returncode is not None
    return subprocess.CompletedProcess(args, process.returncode, out.decode(), err.decode())
