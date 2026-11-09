"""The nightly re-fit of the trader's policy (#399): each cell the cost map names entered at the lowest
θ whose replayed one-share net per signal has a day-level lower bound above 0 (#472), on recorded days
replayed by the paper trader's rules (#470); the sample conditions and the Bonferroni level; the whole
policy's replay checked; the same policy from the same input, no limit in it, and its time and memory
over 20 server-sized days.

Input: an engine database written through the prediction store (predictions as the engine keeps
them), the day's quotes recorded as the collector records them (``trader_doubles.record_quotes``), a
cost map as the Engine API gives it (``tests/trader_doubles.py``). Output: the policy kept in the
trader's store and the job's report. Every price and probability is made up.

The trades worked by hand throughout: the 4-minute barrier at a reference of 70,050 won, tick 100, w 5
(target 70,550, stop 69,550), displayed 2 s after its bar close and bought at the best ask of the
first quote after that, 70,100 (the bar close's standing ask 70,000 is not the buy's). A share: up,
sold at the target once a bid reaches 70,600: 450 won less the commission on 70,100 and 70,550 and the
tax on 70,550 (10.515 + 10.5825 + 141.1) = +287.8025 won, +2.878025 ticks; down, stopped at the bid
69,500: −600 − (10.515 + 10.425 + 139) = −759.94 won, −7.5994 ticks. The policy buys 14 shares of
1,000,000 won (a loss budget of the whole capital, ``trader_doubles.whole_rules``: the trader's own buys
six, ``test_the_replay_buys_what_the_trader_s_loss_budget_pays``): +4,029.235 and −10,639.16 won.
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
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
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
from krx_quant.trader.cells import Cell, open_engine_db
from krx_quant.trader.money import net_of
from krx_quant.trader.paper import (
    BEFORE_ENTRY,
    NOT_UP,
    PAST_LAST_EXIT,
    SPECIAL,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_REACHED,
    Rules,
)
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.refit import (
    BOOTSTRAP,
    FEW_DAYS,
    FEW_TRADES,
    NO_EDGE,
    NO_POLICY_KEPT,
    NOT_ABOVE,
    Grid,
    judge,
    refit_from,
    report,
    run_refit,
)
from krx_quant.trader.replay import OFF_TARGET, Signals, day_reads, target_of
from krx_quant.trader.selection import choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.trader_doubles import (
    CALENDAR,
    DAY,
    FEE,
    EngineApi,
    at,
    cost_map,
    cost_row,
    pick,
    predictor,
    record_quotes,
    whole_rules,
)

BREIMAN = Cell("barrier", "breiman")
FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")
CONSENSUS = Cell("direction", FINAL)
# The hand examples' two days (the calendar guesses weekdays trade), and a grid for their few trades.
SECOND = (2026, 10, 15)
TWO = (DAY, SECOND)
SMALL = Grid(min_days=2, min_trades=2, min_trade_days=2)
UP_SHARE, DOWN_SHARE = net_of(70_100, 70_550, 1, FEE), net_of(70_100, 69_500, 1, FEE)
UP = 14 * UP_SHARE  # a take-profit of 14 shares
DOWN = 14 * DOWN_SHARE  # a stop of 14 shares


def test_the_hand_worked_trades() -> None:
    assert UP_SHARE == pytest.approx(287.8025) and DOWN_SHARE == pytest.approx(-759.94)
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


# The five predictions the policy is fitted on by hand, each of the two days: two rises right above
# 0.7, the others mixed.
FIVE = [
    ("10:00", (0.75, 0.15, 0.10), UP_FIRST, "10:02:00"),  # +2.878025 ticks a share
    ("10:10", (0.55, 0.25, 0.20), DOWN_FIRST, "10:11:00"),  # −7.5994
    ("10:20", (0.65, 0.20, 0.15), DOWN_FIRST, "10:21:00"),  # −7.5994
    ("10:30", (0.45, 0.30, 0.25), UP_FIRST, "10:32:00"),  # +2.878025
    ("10:40", (0.85, 0.10, 0.05), UP_FIRST, "10:42:00"),  # +2.878025
]


def write_five(state: Path, data: Path | None = None, days: Sequence[tuple[int, int, int]] = TWO) -> None:
    """FIVE on each of ``days`` in the engine's database in ``state`` and their quotes recorded under
    ``data`` (by default ``state / "data"``)."""
    store = engine(state)
    try:
        for day in days:
            for close, p, label, reached in FIVE:
                barrier(store, close, p, label, reached=reached, day=day)
    finally:
        store.close()
    quotes = [q for day in days for close, _, label, _ in FIVE for q in quotes_of(close, label, day)]
    record_quotes(data or state / "data", quotes)


def on_days(body: dict[str, Any], days: Sequence[tuple[int, int, int]] = TWO) -> dict[str, Any]:
    """``body`` with its rows' days ``days``."""
    return {**body, "rows": [{**r, "days": [date(*d).isoformat() for d in days]} for r in body["rows"]]}


# The cost map fails breiman: the fit tries it all the same (#472: no pass required).
FAILING = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])
DAYS = on_days(FAILING)


def refit(state: Path, body: dict[str, Any] = DAYS, *, store: Path | None = None, **kwargs: Any) -> Any:
    """``refit_from`` on ``state``'s engine database and recordings, by default at 22:31 of the second
    day into ``state``'s trader store, on the hand examples' grid."""
    args: dict[str, Any] = {
        "rules": whole_rules(),
        "target_version": TARGET_VERSION,
        "grid": SMALL,
        "now": moment(SECOND, "22:31"),
    }
    return refit_from(
        body,
        state / ENGINE_DB,
        store or state / TRADER_DB,
        state / "data",
        CALENDAR.session,
        **(args | kwargs),
    )


# The cells entered by hand on the replayed days.


def test_a_cell_the_cost_map_fails_enters_at_the_lowest_threshold_its_lower_bound_passes(
    tmp_path: Path,
) -> None:
    """Each signal alone a share, each day alike: θ 0.4 (all five) nets (3 × 2.878025 − 2 × 7.5994) ÷ 5
    = −1.312945 ticks a signal; 0.5 (but the 10:30 rise) −2.3606875; 0.6 (10:00, 10:20, 10:40) −0.61445;
    0.7 (10:00, 10:40) and 0.8 (10:40) +2.878025. Five points are judged (two buys over two days at
    0.8 at least), each bound at 99% (1 − 0.05 ÷ 5); the days alike, the bound is the mean. θ 0.7 is the
    lowest above 0, though the cost map fails the cell. Its policy replayed buys the 10:00 and 10:40
    rises each day, 14 shares: 2 × 4,029.235 won a day, above 0: kept, a challenger waiting for a
    cohort behind the empty champion kept before it (#473: the first fit with cells is development
    evidence). The cost map's f* is kept as a diagnosis; the cell has no λ (#471)."""
    write_five(tmp_path)

    done = refit(tmp_path)

    [cell] = done.cells
    assert (cell.kind, cell.role, cell.predictor, cell.threshold) == ("barrier", "top", "breiman", 0.7)
    fitted = cell.fitted
    assert fitted["verdict"] == "fail" and fitted["low"] == 0.60 and fitted["recorded_days"] == 2
    assert "kelly_fraction" not in cell.as_json() and "fraction" not in fitted
    assert fitted["bound"] == {
        "value": pytest.approx(2.878025),
        "low": pytest.approx(2.878025),
        "high": pytest.approx(2.878025),
        "days": 2,
        "level": pytest.approx(0.99),
    }
    signals = fitted["signals"]
    assert list(signals) == ["0.4", "0.5", "0.6", "0.7", "0.8"]
    expected = {"0.4": -1.312945, "0.5": -2.3606875, "0.6": -0.61445, "0.7": 2.878025, "0.8": 2.878025}
    for threshold, mean in expected.items():
        assert signals[threshold]["per_signal"]["value"] == pytest.approx(mean, abs=1e-6), threshold
    assert (signals["0.4"]["bought"], signals["0.4"]["days_bought"], signals["0.8"]["bought"]) == (10, 2, 2)
    assert signals["0.4"]["exits"] == {STOP_LOSS: 4, TAKE_PROFIT: 6}
    assert (fitted["trades"], fitted["wins"], fitted["net"]) == (4, 4, pytest.approx(4 * UP))
    assert fitted["read"] == {"read": 10, "rises": 10}
    assert done.fit["judged"] == {"points": 5, "level": pytest.approx(0.99)}
    assert done.fit["result"]["per_day"] == {
        "value": pytest.approx(2 * UP),
        "low": pytest.approx(2 * UP),
        "high": pytest.approx(2 * UP),
        "level": 0.95,
    }
    assert done.fit["result"]["days"] == 2 and done.fit["result"]["growth"] == pytest.approx(
        4 * math.log1p(UP / CAPITAL)
    )
    assert done.fit["replay"] == {"days": 2, "no_recording": [], "gaps": [], "passes": 2, "days_read": 4}
    assert done.fit["cost_map"]["named"] == 1 and done.fit["cost_map"]["passed"] == 0
    assert done.left_out == () and done.rejected is None
    assert (done.version, done.champion, done.champion_version, done.bootstrap) == (2, False, 1, 1), (
        "the first with cells waits behind an empty champion (#473)"
    )
    with closing(sqlite3.connect(tmp_path / TRADER_DB)) as db:
        assert db.execute("SELECT id, cells FROM policies WHERE id = 1").fetchone() == (1, "[]")
        assert db.execute("SELECT policy_id, reason FROM champions").fetchall() == [(1, BOOTSTRAP)]
        assert policies.waiting_versions(db) == [2]
    assert report(done).startswith(
        "정책 맞추기 (#399): 정책 2 · 기다리는 정책(#473: 다음 묶음을 기다린다. 검토 날 밤에 기다리는 "
        "정책 가운데 새것부터 셋까지 다음 주기의 도전 정책으로 들어간다) · 챔피언이 없어 빈 정책 1을 "
        "챔피언으로 먼저 두었다\n"
    )


def test_each_cell_s_calibration_counts_its_rises_by_score_with_no_price(tmp_path: Path) -> None:
    """#475 (spec #468 8 "확률 보정"): the five rises of each day fall one in each bin of the grid's θ
    (0.45, 0.55, 0.65, 0.75, 0.85): those reaching the upper barrier first (10:30, 10:00, 10:40) have
    the class right, reached the target first and netted above 0 bought alone a share; the others fell
    first and lost. Kept in the cell's fit (``GET /v1/trader/policies``) and printed under it."""
    write_five(tmp_path)

    done = refit(tmp_path)

    [cell] = done.cells
    calibration = cell.fitted["calibration"]
    assert calibration["below"] == 0
    assert [(b["from"], b["to"]) for b in calibration["bins"]] == [
        (0.4, 0.5),
        (0.5, 0.6),
        (0.6, 0.7),
        (0.7, 0.8),
        (0.8, 1.0),
    ]
    rose = {"up": 2, "flat": 0, "down": 0}
    fell = {"up": 0, "flat": 0, "down": 2}
    assert [
        (b["signals"], b["answered"], b["outcomes"], b["hits"], b["first"], b["bought"], b["profitable"])
        for b in calibration["bins"]
    ] == [
        (2, 2, rose, 2, 2, 2, 2),
        (2, 2, fell, 0, 0, 2, 0),
        (2, 2, fell, 0, 0, 2, 0),
        (2, 2, rose, 2, 2, 2, 2),
        (2, 2, rose, 2, 2, 2, 2),
    ]
    printed = report(done).splitlines()
    head = printed.index(
        "   보정 (#475, 점수 구간마다 오름 신호, 1주씩 따로 잰 것: 설명일 뿐 θ를 바꾸지 않는다)"
    )
    assert printed[head + 1 : head + 3] == [
        "   - [0.4, 0.5): 신호 2 · 결과 상승 100%, 보합 0%, 하락 0%(답 2) · 클래스 맞음 100% · "
        "목표 먼저 100% · 순이익 100%(체결 2)",
        "   - [0.5, 0.6): 신호 2 · 결과 상승 0%, 보합 0%, 하락 100%(답 2) · 클래스 맞음 0% · "
        "목표 먼저 0% · 순이익 0%(체결 2)",
    ]
    assert printed[head + 5].startswith("   - [0.8, 1]: 신호 2")
    assert not re.search(r"70[,.]?[0-9]{3}", report(done)), "no price"


def test_the_replay_buys_what_the_trader_s_loss_budget_pays(tmp_path: Path) -> None:
    """By the trader's rules (#471): a share bought at 70,100 loses 70,100 − 69,550 (the stop) + 10.515 +
    69,550 × 0.215% + a tick of 100 = 810.0475 won at its stop; 0.5% of 1,000,000 won buys
    ⌊5,000 ÷ 810.0475⌋ = 6. The cell is entered on its signals a share alike."""
    write_five(tmp_path)

    done = refit(tmp_path, rules=Rules(fee_rate=FEE))

    [cell] = done.cells
    assert cell.threshold == 0.7
    assert cell.fitted["net"] == pytest.approx(4 * 6 * UP_SHARE), "six shares a take-profit"


# The judgement: the same signals, the same points; the sample conditions; Bonferroni.

DAY_ONE = date(2026, 10, 1)


def measured(per_day: Sequence[float], bought: Sequence[int] | int = 1) -> Signals:
    """A cell's signals at a θ: on day i one signal measured whose net is ``per_day[i]`` ticks, and
    ``bought[i]`` signals bought (an int: the same each day)."""
    counts = [bought] * len(per_day) if isinstance(bought, int) else list(bought)
    s = Signals()
    for i, (value, n) in enumerate(zip(per_day, counts, strict=True)):
        day = DAY_ONE + timedelta(days=i)
        s.days[day] = [max(1, n), value * max(1, n)]
        if n:
            s.bought_on[day] = n
    s.bought = sum(counts)
    s.signals = sum(max(1, n) for n in counts)
    return s


def at_every(
    signals: Signals, thresholds: Sequence[float] = (0.4, 0.5, 0.6, 0.7, 0.8)
) -> dict[float, Signals]:
    return {t: signals for t in thresholds}


def test_the_same_signals_enter_the_same_cells_and_one_whose_bound_is_not_above_0_is_left_out() -> None:
    """Two cells measured alike on 20 days: breiman's nets +1 tick a signal every day (its bound +1
    at any level), the baseline's −0.5. Judged twice, the same; the baseline is left out (``no_edge``),
    its best point the lowest θ (all alike)."""
    picks = [pick(BREIMAN), pick(FIFTEEN, role="baseline")]
    signals = {BREIMAN: at_every(measured([1.0] * 20)), FIFTEEN: at_every(measured([-0.5] * 20))}
    recorded = {BREIMAN: 20, FIFTEEN: 20}

    first, second = (judge(picks, signals, recorded, Grid()) for _ in range(2))

    assert first == second
    [kept] = first.kept
    assert (kept.pick.cell, kept.threshold) == (BREIMAN, 0.4) and kept.bound.low == pytest.approx(1.0)
    [out] = first.left_out
    assert (out["predictor"], out["why"], out["best"]["threshold"]) == (FIFTEEN.predictor, NO_EDGE, 0.4)
    assert out["best"]["bound"]["low"] == pytest.approx(-0.5)
    assert first.judged == 10 and first.level == pytest.approx(0.995)


def test_the_sample_conditions_twenty_days_twenty_trades_on_ten_days() -> None:
    """Nets of +1 tick a signal throughout; the evidence decides. 19 days recorded: ``few_days``. 20 days,
    19 bought: ``few_trades``. 20 bought on 9 days (seven days of two, two of three): ``few_trades``. 20
    bought on 10 days: judged, and kept."""
    cells = {name: Cell("barrier", name) for name in ("short", "few", "bunched", "enough")}
    picks = [pick(c) for c in cells.values()]
    signals = {
        cells["short"]: at_every(measured([1.0] * 19)),
        cells["few"]: at_every(measured([1.0] * 20, [1] * 19 + [0])),
        cells["bunched"]: at_every(measured([1.0] * 20, [2] * 7 + [3] * 2 + [0] * 11)),
        cells["enough"]: at_every(measured([1.0] * 20, [2] * 10 + [0] * 10)),
    }
    recorded = {cells["short"]: 19, cells["few"]: 20, cells["bunched"]: 20, cells["enough"]: 20}

    done = judge(picks, signals, recorded, Grid())

    assert [p.pick.cell for p in done.kept] == [cells["enough"]]
    why = {x["predictor"]: x for x in done.left_out}
    assert (why["short"]["why"], why["short"]["recorded_days"]) == (FEW_DAYS, 19)
    assert (why["few"]["why"], why["few"]["most_trades"], why["few"]["most_days"]) == (FEW_TRADES, 19, 19)
    bunched = why["bunched"]
    assert (bunched["why"], bunched["most_trades"], bunched["most_days"]) == (FEW_TRADES, 20, 9)
    assert done.judged == 5, "the five points of the one cell with the evidence"


# Twenty days, each the net per signal of one: ten of +0.6 ticks, ten of −0.2. Their mean is +0.2, the
# days' standard deviation 0.41039 and standard error 0.091766. At 95% (t of 19 degrees 2.0930) the
# lower bound is +0.0079; at 99% (one of five points judged, t 2.8609) it is −0.0625.
WEAK = [0.6] * 10 + [-0.2] * 10


def test_every_cell_and_threshold_judged_shares_the_error_bonferroni() -> None:
    """breiman's weak edge, judged at its θ 0.8 alone (too few trades below it): the one point judged,
    its bound at 95% is +0.0079: kept. With the baseline judged at four θ besides, each bound is at
    1 − 0.05 ÷ 5 = 99%: breiman's −0.0625 is not above 0 (``no_edge``), whatever the baseline's."""
    weak = {0.8: measured(WEAK)} | {t: measured(WEAK, 0) for t in (0.4, 0.5, 0.6, 0.7)}
    alone = judge([pick(BREIMAN)], {BREIMAN: weak}, {BREIMAN: 20}, Grid())
    strong = at_every(measured([2.0] * 20), (0.4, 0.5, 0.6, 0.7))
    both = judge(
        [pick(BREIMAN), pick(FIFTEEN, role="baseline")],
        {BREIMAN: weak, FIFTEEN: strong},
        {BREIMAN: 20, FIFTEEN: 20},
        Grid(),
    )

    [kept] = alone.kept
    assert (alone.judged, kept.threshold) == (1, 0.8) and alone.level == pytest.approx(0.95)
    assert kept.bound.low == pytest.approx(0.0079309, abs=1e-6)
    assert both.judged == 5 and both.level == pytest.approx(0.99)
    assert [p.pick.cell for p in both.kept] == [FIFTEEN]
    [out] = both.left_out
    assert (out["predictor"], out["why"]) == ("breiman", NO_EDGE)
    assert out["best"] == {
        "threshold": 0.8,
        "bound": {
            "value": pytest.approx(0.2),
            "low": pytest.approx(-0.0625374, abs=1e-6),
            "high": pytest.approx(0.4625374, abs=1e-6),
            "days": 20,
            "level": pytest.approx(0.99),
        },
    }


def test_the_lowest_threshold_above_0_is_kept_and_the_kept_are_ranked_by_their_bound() -> None:
    """breiman nets −1 at 0.4, +1 at 0.5 and +3 at 0.6 and up: kept at 0.5, its bound +1. The baseline
    nets +2 throughout: kept at 0.4, its bound +2, it ranks first."""
    lows = {0.4: -1.0, 0.5: 1.0, 0.6: 3.0, 0.7: 3.0, 0.8: 3.0}
    picks = [pick(BREIMAN), pick(FIFTEEN, role="baseline")]
    signals = {
        BREIMAN: {t: measured([v] * 20) for t, v in lows.items()},
        FIFTEEN: at_every(measured([2.0] * 20)),
    }

    done = judge(picks, signals, {BREIMAN: 20, FIFTEEN: 20}, Grid())

    assert [(p.pick.cell, p.threshold) for p in done.kept] == [(FIFTEEN, 0.4), (BREIMAN, 0.5)]
    assert [p.bound.low for p in done.kept] == pytest.approx([2.0, 1.0])


# The probability is a score alone.


def test_a_predictor_always_at_1_000_is_judged_on_its_replayed_net_alone(tmp_path: Path) -> None:
    """breiman's rises are all at 1.000: every θ's signals are the same. Each a time exit at the bid
    70,000 of the quote before its barrier, bought at 70,100: −261.015 won, −2.61015 ticks a share, every
    day. No θ's bound is above 0: left out (``no_edge``), whatever its probability."""
    store = engine(tmp_path)
    try:
        for day in TWO:
            for close in ("10:00", "10:10"):
                barrier(store, close, (1.0, 0.0, 0.0), TIMEOUT, day=day)
    finally:
        store.close()
    record_quotes(
        tmp_path / "data",
        [q for d in TWO for close in ("10:00", "10:10") for q in quotes_of(close, TIMEOUT, d)],
    )

    done = refit(tmp_path)

    assert done.cells == ()
    [out] = done.left_out
    assert (out["why"], out["best"]["threshold"]) == (NO_EDGE, 0.4)
    assert out["best"]["bound"]["low"] == pytest.approx(net_of(70_100, 70_000, 1, FEE) / 100)
    assert len({json.dumps(s) for s in out["signals"].values()}) == 1, "every θ the same signals"
    assert out["signals"]["0.8"]["bought"] == 4
    assert done.version == 1 and done.fit["replay"]["passes"] == 1, "no cell kept: an empty policy"


# The whole policy checked.


def cooling_day(day: tuple[int, int, int]) -> list[BestQuote]:
    """A day of twelve rises a minute apart from 10:00, each displayed at :02 and bought at 70,100 on
    the quote at :03. The first three stopped at the bid 69,500 a minute later, the nine others sold
    at the target once the bid reaches 70,600 at :30."""
    quotes = []
    for i in range(12):
        t = moment(day, f"10:{i:02d}")
        quotes.append(BestQuote(t + timedelta(seconds=3), 70_000, 70_100, 10.0, 10.0))
        if i < 3:
            quotes.append(BestQuote(t + timedelta(minutes=1), 69_500, 69_600, 10.0, 10.0))
        else:
            quotes.append(BestQuote(t + timedelta(seconds=30), 70_600, 70_700, 10.0, 10.0))
    return sorted(quotes, key=lambda q: q.received_at)


def test_a_policy_whose_whole_replay_has_no_lower_bound_above_0_is_not_kept(tmp_path: Path) -> None:
    """Alone a share, breiman's twelve rises a day net (3 × −7.5994 + 9 × 2.878025) ÷ 12 = +0.2586688
    ticks a signal, each day: kept at 0.4. Replayed as the policy, its first three are three net losses
    in a row by 10:03 (−759.94 won a share): the nine others cool down for 30 minutes (#471). The first
    day buys 14, 14 and 13 shares (the cash after two losses: ⌊978,721.68 ÷ 70,110.515⌋), −31,157.54
    won; the second 13 each, −29,637.66 won. Their mean is −30,397.60 won a day, its lower bound below
    0: not kept, and said why. No store is made for it."""
    store = engine(tmp_path)
    try:
        for day in TWO:
            for i in range(12):
                barrier(store, f"10:{i:02d}", (0.85, 0.10, 0.05), DOWN_FIRST if i < 3 else UP_FIRST, day=day)
    finally:
        store.close()
    record_quotes(tmp_path / "data", [q for d in TWO for q in cooling_day(d)])

    done = refit(tmp_path)

    assert (done.version, done.rejected, done.champion_version) == (None, NOT_ABOVE, None)
    [cell] = done.cells
    assert cell.threshold == 0.4 and cell.fitted["bound"]["low"] == pytest.approx(0.2586688, abs=1e-6)
    assert cell.fitted["trades"] == 6 and cell.fitted["net"] == pytest.approx(80 * DOWN_SHARE)
    per_day = done.fit["result"]["per_day"]
    assert per_day["value"] == pytest.approx(40 * DOWN_SHARE) and per_day["low"] < 0
    assert not (tmp_path / TRADER_DB).exists(), "nothing kept, no store made"
    text = report(done)
    assert text.startswith(
        "정책 맞추기 (#399): 새 정책 없음 · 실행 손익 하한을 넘은 1칸으로 짠 정책 전체의 재생에서 날마다 "
        "순손익의 95% 하한이 0을 넘지 않아 저장하지 않았다(#472, policy_not_above)\n"
    )
    assert "날마다 평균 -30,398원(95% 하한 -" in text


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

    done = refit(tmp_path, on_days(DAYS, [DAY]), grid=Grid(min_days=1, thresholds=(0.6,)))

    [out] = done.left_out
    assert out["read"] == {
        "read": 9,
        "rises": 4,
        BEFORE_ENTRY: 1,
        NOT_UP: 1,
        OFF_TARGET: 1,
        PAST_LAST_EXIT: 1,
        SPECIAL: 1,
    }
    alone = out["signals"]["0.6"]
    assert (alone["signals"], alone["bought"], alone["refused"]) == (4, 3, {TARGET_REACHED: 1})
    assert out["why"] == FEW_TRADES, "three bought on one day: under the twenty"


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
    the values of a policy waiting (#473): not kept (#400); nor once that is the champion."""
    write_five(tmp_path)

    first = refit(tmp_path)
    other = refit(tmp_path, store=tmp_path / "other" / TRADER_DB, now=moment(SECOND, "22:45"))
    second = refit(tmp_path, now=moment(SECOND, "22:50"))

    assert (first.version, first.bootstrap, other.version, other.bootstrap) == (2, 1, 2, 1)
    assert other.cells == first.cells and other.left_out == first.left_out
    assert {**other.fit, "run": None} == {**first.fit, "run": None}, "but its time and memory"
    stored = []
    for path in (tmp_path / TRADER_DB, tmp_path / "other" / TRADER_DB):
        with closing(sqlite3.connect(path)) as db:
            stored.append([r[0] for r in db.execute("SELECT cells FROM policies ORDER BY id")])
            champions = db.execute("SELECT policy_id, reason FROM champions").fetchall()
        assert champions == [(1, BOOTSTRAP)]
    assert stored[0] == stored[1], "byte for byte, and two policies each: the empty champion and the fit"
    assert (second.version, second.same_as, second.champion, second.champion_version) == (2, 2, False, 1)
    assert report(second).startswith(
        "정책 맞추기 (#399): 새 정책 없음 · 맞춘 값이 기다리는 정책 정책 2과 같아 저장하지 않았다\n"
    )
    with closing(policies.connect(tmp_path / TRADER_DB)) as db:
        assert policies.crown(db, moment(SECOND, "22:55"), 2, "test", {})
    third = refit(tmp_path, now=moment(SECOND, "23:00"))
    assert (third.version, third.same_as) == (2, 2)
    assert report(third).startswith("정책 맞추기 (#399): 새 정책 없음 · 맞춘 값이 챔피언 정책 2과 같아")


def test_no_cell_kept_makes_an_empty_policy(tmp_path: Path) -> None:
    """breiman, named but never predicting, has no day recorded (``few_days``): the policy is empty."""
    PredictionStore(tmp_path / ENGINE_DB).close()

    done = refit(tmp_path, FAILING)

    assert (done.cells, done.named, done.champion) == ((), 1, True)
    assert [(x["predictor"], x["why"], x["recorded_days"]) for x in done.left_out] == [
        ("breiman", FEW_DAYS, 0)
    ]
    text = report(done)
    assert "매매할 칸: 없음\n" in text + "\n"
    assert (
        "뺀 칸: barrier breiman (top): few_days, 녹화한 날이 2일에 못 미침(0일) · 비용 지도 판정 fail" in text
    )


def test_a_cost_map_naming_no_cell_makes_an_empty_policy_without_a_replay(tmp_path: Path) -> None:
    PredictionStore(tmp_path / ENGINE_DB).close()

    done = refit(tmp_path, cost_map([cost_row("barrier")]))

    assert (done.cells, done.left_out, done.named, done.champion) == ((), (), 0, True)
    assert done.fit["replay"]["days"] == 0 and done.fit["replay"]["passes"] == 0
    assert "매매할 칸: 없음 (비용 지도에 이름이 있는 칸이 없다)" in report(done)


def test_a_day_without_its_recording_is_not_counted(tmp_path: Path) -> None:
    """The five predictions of two days, the second day's recording missing: one day recorded, under
    the two (``few_days``); its signals left out (``no_recording``), none a trade."""
    write_five(tmp_path, days=[DAY])
    store = engine(tmp_path)
    try:
        for close, p, label, reached in FIVE:
            barrier(store, close, p, label, reached=reached, day=SECOND)
    finally:
        store.close()

    done = refit(tmp_path)

    [out] = done.left_out
    assert (out["why"], out["recorded_days"]) == (FEW_DAYS, 1)
    assert done.fit["replay"]["no_recording"] == [date(*SECOND).isoformat()]
    assert out["signals"]["0.4"]["missing"] == {"no_recording": 5}
    assert "(녹화 없음 1일, 장중 결측 구간 0일)" in report(done)


# No limit in a policy; the fit changes none.


def test_a_policy_holds_no_limit_and_the_fit_changes_none(tmp_path: Path) -> None:
    write_five(tmp_path)
    narrow = TraderSettings(capital=500_000)

    done = refit(tmp_path, rules=whole_rules(capital=narrow.capital))

    stored = policies.read_policies(tmp_path / TRADER_DB, 10)["policies"][0]
    words = json.dumps(stored).lower().replace(STOP_LOSS, "exit")  # the signals' exits: no limit
    assert not re.search(r"capital|limit|loss|한도|상한", words), "no limit, nor the capital, in a policy"
    assert set(stored["cells"][0]) == {"kind", "role", "predictor", "threshold", "fitted"}
    assert narrow == TraderSettings(capital=500_000), "the trader's settings as they were"
    # 500,000 won buys 7 shares at 70,100: the 4 take-profits make 4 × 7 × 287.8025 won.
    assert done.cells[0].fitted["net"] == pytest.approx(4 * 7 * UP_SHARE)


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
        code = await run_refit(settings, TraderSettings(engine_url=url), moment(SECOND, "22:31"))

    assert code == 0
    assert api.asked == [{"symbol": SYMBOL, "period": "20d", "special": "exclude"}]
    out = capsys.readouterr().out
    assert out.startswith(
        "도전 정책 판정 (#473: 묶음을 검토 날 밤에 한 번)\n챔피언이 아직 없다(첫 밤 정책 맞추기 전).\n\n"
    ), "the challengers are judged first (#400): none before the first champion"
    assert "\n정책 맞추기 (#399): 정책 1 · 챔피언(처음 맞춘 정책)\n" in out
    assert out.endswith(
        "\n도전 정책 묶음 (#473): 주기 1(2026-10-16–2026-11-12)의 묶음: 기다리는 정책이 없어 "
        "비었다(이 주기에는 도전 정책이 돌지 않는다)\n"
    ), "the night's end: its evaluation's first cycle, ahead, has its cohort (none waits)"
    assert (
        "비용 지도: 005930 20d, as_of 2026-10-14T08:30:00+09:00, 이름이 있는 칸 1칸(판정과 상관없이 모두 "
        "시험한다. 시장가 왕복 통과 0칸은 참고만)" in out
    )
    assert (
        "뺀 칸: barrier breiman (top): few_days, 녹화한 날이 20일에 못 미침(2일) · 비용 지도 판정 fail" in out
    )
    assert (
        "진입 (#472): θ 0.4 0.5 0.6 0.7 0.8 가운데 1주씩 따로 잰 신호당 순손익의 날 단위 하한이 0을 넘는 "
        "가장 낮은 θ · 녹화한 날 20일 이상, 체결 20건 이상이 10일 이상에 · 하한의 수준 95%(판정한 칸 × θ "
        "0개에 Bonferroni)" in out
    )
    assert (
        "재생 (#470): 녹화한 날을 trader의 모의 체결 규칙 그대로 다시 돌린다 · 2일(녹화 없음 0일, 장중 결측 "
        "구간 0일) · 재생 1번, 날 읽기 2번" in out
    )
    assert "   1주씩 따로 잰 신호: θ 0.4 신호 10, 체결 10, 신호당 " in out
    assert re.search(r"메모리: 최대 RSS (\d+\.\d|\?) MB", out), out
    assert not re.search(r"(69|70)[,.]?[0-9]{3}", out), "no price in the output"
    [kept] = policies.read_policies(tmp_path / TRADER_DB, 10)["policies"]
    assert kept["cells"] == [] and kept["left_out"][0]["why"] == FEW_DAYS, (
        "the server's grid wants 20 days recorded: two are not enough"
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
# The nine cells the server's cost map names (#472: every one tried): the direction's three, each
# barrier's top algorithm and baseline.
SERVER_CELLS = (
    (Cell("direction", FINAL), "consensus"),
    (Cell("direction", "logit"), "top"),
    (Cell("direction", "persistence"), "baseline"),
    (Cell("barrier", "breiman"), "top"),
    (Cell("barrier", "barrier-persist"), "baseline"),
    (Cell("barrier_15m", "breiman-15m"), "top"),
    (Cell("barrier_15m", "barrier-persist-15m"), "baseline"),
    (Cell("barrier_30m", "breiman-30m"), "top"),
    (Cell("barrier_30m", "barrier-persist-30m"), "baseline"),
)
# The integrated best quotes of a recorded day on the server: about 570,000 (the book's and the
# trades', as #432's measurement counted them).
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
    """Twenty days of the nine cells' predictions and answers in the engine's database, written in
    bulk; and a cost map naming them over those days."""
    store = PredictionStore(state / ENGINE_DB)
    tops = [(c, role) for c, role in SERVER_CELLS if role == "top"]
    store.record_models(
        [(c.predictor, "v1", target) for c, role in tops if (target := target_of(c, role, TARGET_VERSION))]
    )
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
            for kind in sorted({c.kind for c, _ in SERVER_CELLS if c.kind != "direction"}):
                answers.append((kind, SYMBOL, t.isoformat(), BARRIER_VERSION, label, json.dumps(path)))
            for cell, _ in SERVER_CELLS:
                if cell.kind == "direction":
                    pid += 1
                    predictions.append((pid, SYMBOL, cell.predictor, "v1", t.isoformat(), t.isoformat(), *p))
                    outcomes.append((pid, (t + timedelta(minutes=4)).isoformat(), 70_050.0, 5, t.isoformat()))
                    continue
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
    rows = []
    for kind in ("direction", "barrier", "barrier_15m", "barrier_30m"):
        named: dict[str, Any] = {
            r: predictor(c.predictor, 0.55, "fail") for c, r in SERVER_CELLS if c.kind == kind
        }
        rows.append({**cost_row(kind, **named), "days": days})
    return cost_map(rows)


def timed_refit(state: Path, body: dict[str, Any]) -> Any:
    return refit_from(
        body,
        state / ENGINE_DB,
        state / TRADER_DB,
        state / "data",
        CALENDAR.session,
        rules=whole_rules(),
        target_version=TARGET_VERSION,
        grid=Grid(),
        now=datetime.combine(TWENTY[-1], time(22, 31), KST),
    )


# Tracing every allocation slows the replay several times over (about 40 s here with the fixture).
@pytest.mark.timeout(300)
def test_twenty_server_sized_days_are_replayed_in_little_memory(tmp_path: Path) -> None:
    """Twenty days of 379 bar closes, nine cells, 570,000 quotes a day: 68,220 predictions, the nine
    measured alone in one pass a day's recording at a time (and the policy kept, if any, in one more).
    The Python memory the fit allocates at its peak, a day's quotes included, stays under 64 MB. The
    process's resident peak is the job's own figure (``test_the_job_prints_its_peak_resident_memory``)."""
    body = server_sized(tmp_path)
    for day in TWENTY:
        record_day(tmp_path / "data", day, SERVER_QUOTES)
    assert len(choose(body).named) == 9

    tracemalloc.start()
    try:
        done = timed_refit(tmp_path, body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    read = sum(c.fitted["read"]["read"] for c in done.cells) + sum(x["read"]["read"] for x in done.left_out)
    assert read == 9 * 20 * len(CLOSES)
    passes = 2 if done.cells else 1
    assert done.fit["replay"]["passes"] == passes and done.fit["replay"]["days_read"] == 20 * passes
    assert set(done.fit["run"]["days"]) == {d.isoformat() for d in TWENTY}, "each day's peak"
    print(f"refit over 20 server-sized days: {peak / 1024 / 1024:.1f} MB traced at the peak")
    assert peak < 64 * 1024 * 1024, f"{peak / 1024 / 1024:.1f} MB"


@pytest.mark.timeout(120)  # a process of its own: the CLI's imports, then the passes over the 20 days
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


# Each cell is measured once a night.


def test_the_cells_are_measured_alone_in_one_pass(tmp_path: Path) -> None:
    """breiman and the 15-minute baseline both named: one pass measures both alone, and with neither
    kept no policy pass follows."""
    write_five(tmp_path)
    body = on_days(
        cost_map(
            [
                cost_row("barrier", top=predictor("breiman", 0.60, "fail")),
                cost_row("barrier_15m", baseline=predictor(FIFTEEN.predictor, 0.55, "few")),
            ]
        )
    )

    done = refit(tmp_path, body, grid=Grid(min_days=2, min_trades=50))

    assert done.cells == ()
    assert [(x["predictor"], x["why"]) for x in done.left_out] == [
        ("breiman", FEW_TRADES),
        (FIFTEEN.predictor, FEW_TRADES),
    ]
    assert done.fit["replay"]["passes"] == 1 and done.fit["replay"]["days_read"] == 2
    assert Counter(x["verdict"] for x in done.left_out) == {"fail": 1, "few": 1}
