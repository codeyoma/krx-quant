"""Where an algorithm errs (#431, ``krx-quant error-breakdown``).

Input: a store holding hand-made predictions of turing and the climate baseline, their bar closes'
touch answers (one or two ticks of spread) and the barrier's σ̂₄ there, over two days, with a
special prediction and one by a model of another target that the leaderboard leaves out. Output:
each cell's confusion matrix, precisions and lift over the climate baseline, worked out by hand
below; the text, exploratory at its head, without prices; and the scheduled job that posts it.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.barriers import BARRIER
from krx_quant.baselines import Forecast, baselines_for
from krx_quant.climate import CLIMATE
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.consensus import NAMES
from krx_quant.error_breakdown import (
    NO_SIGMA,
    ONE_TICK,
    WIDER,
    Cell,
    breakdown,
    error_breakdown,
    read,
)
from krx_quant.github import GitHub
from krx_quant.kinds import Answer
from krx_quant.predictions import NewPrediction, PredictionStore, Score, read_only, scored_rows
from krx_quant.pricing import Direction
from krx_quant.scheduled import (
    ERROR_BREAKDOWN,
    INTERRUPTED,
    JOBS,
    OK,
    STATE_FILE,
    Facts,
    JobRecord,
    JobState,
    Night,
    Scheduler,
)
from krx_quant.stats import leaderboard, on_target, scored_by_algorithm, split
from krx_quant.touch import TARGET_VERSION
from tests.fake_github import REPO, TOKEN, fake_github
from tests.test_leaderboard_api import touch
from tests.test_scheduled import AT, CALENDAR, COMMIT, FRIDAY, MONDAY, SATURDAY, Told, at

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
D1, D2 = date(2026, 10, 12), date(2026, 10, 13)
SYMBOL = "005930"
MIN_SAMPLE = 5
SETTINGS = Settings("key", "secret", min_sample=MIN_SAMPLE, target_version=TARGET_VERSION)

# (day, bar close, turing's call, the climate's (None: it made none), the touch answer, the spread
# in ticks, σ̂₄ in ticks (None: w = k)). Every bar close is at 81,250 won, tick 100.
ROWS = [
    (D1, "09:01", UP, FLAT, UP, 1, 1.0),  # gain +1
    (D1, "09:02", UP, FLAT, FLAT, 1, 2.0),  # -1
    (D1, "09:03", DOWN, FLAT, DOWN, 2, 3.0),  # +1
    (D1, "09:04", FLAT, FLAT, FLAT, 1, 4.0),  # 0
    (D1, "09:31", UP, UP, DOWN, 2, 5.0),  # 0
    (D1, "09:32", DOWN, UP, DOWN, 1, 6.0),  # +1
    (D2, "09:01", UP, FLAT, UP, 1, 7.0),  # +1
    (D2, "09:02", FLAT, FLAT, UP, 1, None),  # 0
    (D2, "09:31", DOWN, DOWN, FLAT, 2, 8.0),  # 0
    (D2, "09:32", UP, DOWN, UP, 1, 9.0),  # +1
    (D2, "09:33", UP, None, UP, 1, 10.0),  # no pair
]
T_975_DF1 = 12.706204736  # Student's t, 97.5% quantile, one degree of freedom


def predict(
    store: PredictionStore,
    algorithm: str,
    t: datetime,
    call: Direction,
    answer: Direction,
    *,
    version: str = "v1",
    special: bool = False,
) -> None:
    """One sure call at ``t``, scored once its four minutes are over."""
    [pending] = store.add_all(
        [
            NewPrediction(
                SYMBOL,
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
    store.score_all([(pending.prediction_id, Score(81_250, answer, answer, None))], t + timedelta(minutes=4))


def hand_made(db: Path) -> None:
    store = PredictionStore(db)
    store.record_models([("turing", "v1", TARGET_VERSION), ("turing", "old", 2)])
    for day, hhmm, turing, climate, answer, spread, sigma in ROWS:
        t = at(day, hhmm)
        predict(store, "turing", t, turing, answer)
        if climate is not None:
            predict(store, CLIMATE, t, climate, answer, version="baseline-climate-default")
        label = touch(SYMBOL, t, answer)
        if spread == 2:
            label = replace(label, bid_start=81_150, ask_start=81_350)
        store.add_touches([label])
        path = {"w": 2, "sigma": sigma, "k": 2, "tick": 100, "anchor": 81_250}
        store.add_kind_answers(
            BARRIER.name, BARRIER.answer_version, [(SYMBOL, t, Answer("timeout", None, path), False)]
        )
    # Left out, as the leaderboard leaves them out: a special window, and a model of another target.
    special, old = at(D2, "09:34"), at(D2, "09:35")
    predict(store, "turing", special, UP, DOWN, special=True)
    predict(store, CLIMATE, special, DOWN, DOWN, version="baseline-climate-default", special=True)
    predict(store, "turing", old, UP, DOWN, version="old")
    predict(store, CLIMATE, old, DOWN, DOWN, version="baseline-climate-default")
    store.add_touches([touch(SYMBOL, special, DOWN), touch(SYMBOL, old, DOWN)])
    store.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "engine.sqlite3"
    hand_made(path)
    return path


def cells(db: Path) -> dict[str, dict[str, Cell]]:
    with read_only(db) as conn:
        assert conn is not None
        data = read(conn, SETTINGS, D2, 20, "turing")
    return {
        title.split(".")[0]: {c.name: c for c in found}
        for title, found in breakdown(data, "turing", 0.00015, MIN_SAMPLE)
    }


def confusion(c: Cell) -> dict[str, list[int]]:
    """Each called class's row: how many answers were up, flat and down."""
    return {called.value: [c.confusion[called][d] for d in Direction] for called in Direction}


def normal_low(gains: Sequence[float]) -> float:
    n, mean = len(gains), sum(gains) / len(gains)
    sd = math.sqrt(sum((g - mean) ** 2 for g in gains) / (n - 1))
    return mean - 1.959963984540054 * sd / math.sqrt(n)


def test_the_whole_period_s_confusion_matrix_and_lift_are_the_hand_worked_ones(db: Path) -> None:
    [whole] = cells(db)["1"].values()
    # The special prediction and the old target's are left out: eleven scored, ten paired.
    assert (whole.entry.summary.n, whole.lift.n, whole.days) == (11, 10, 2)
    assert confusion(whole) == {"up": [4, 1, 1], "flat": [1, 1, 0], "down": [0, 1, 2]}
    assert whole.entry.summary.accuracy is not None and whole.entry.summary.accuracy.value == 7 / 11
    precision = {d.value: whole.precision(d) for d in Direction}
    assert {k: (i.value if i else None) for k, i in precision.items()} == {
        "up": 4 / 6,
        "flat": 1 / 2,
        "down": 2 / 3,
    }
    assert whole.climate is not None and whole.climate.value == 2 / 10  # 09:02 and 09:04 on day 1
    gains = [1, -1, 1, 0, 0, 1, 1, 0, 0, 1]
    assert whole.lift.value == pytest.approx(0.4)
    assert whole.lift.low == pytest.approx(normal_low(gains))
    assert whole.lift.p is None  # fewer than 30 pairs: not tested
    # By trading day: day 1's mean gain 2/6, day 2's 2/4; their mean 5/12, its standard error 1/12.
    assert whole.by_day is not None and whole.by_day.n == 2
    assert whole.by_day.value == pytest.approx(5 / 12)
    assert whole.by_day.low == pytest.approx(5 / 12 - T_975_DF1 / 12, abs=1e-6)
    assert whole.enough


def test_the_cells_split_by_slot_spread_and_volatility_as_worked_by_hand(db: Path) -> None:
    found = cells(db)
    assert list(found["2"]) == [SYMBOL]
    slots = found["3"]
    assert list(slots) == ["09:00", "09:30"]
    assert (slots["09:00"].entry.summary.n, slots["09:00"].lift.n) == (6, 6)
    late = slots["09:30"]
    assert (late.entry.summary.n, late.lift.n) == (5, 4)
    assert confusion(late) == {"up": [2, 0, 1], "flat": [0, 0, 0], "down": [0, 1, 1]}
    assert late.lift.value == pytest.approx(2 / 4)
    # Both days gained 1/2: no spread, so the interval is that gain alone.
    assert late.by_day is not None and (late.by_day.value, late.by_day.low) == (0.5, 0.5)
    assert not late.enough  # four pairs, under five

    spreads = found["4"]
    assert list(spreads) == [ONE_TICK, WIDER]  # no locked or crossed quote, none without quotes
    wide = spreads[WIDER]  # 09:03 and 09:31 on day 1, 09:31 on day 2
    assert confusion(wide) == {"up": [0, 0, 1], "flat": [0, 0, 0], "down": [0, 1, 1]}
    assert wide.lift.value == pytest.approx(1 / 3)
    assert wide.by_day is not None and wide.by_day.value == pytest.approx((1 / 2 + 0) / 2)
    assert (spreads[ONE_TICK].entry.summary.n, spreads[ONE_TICK].lift.n) == (8, 7)

    # σ̂₄ of the ten bar closes with one, 1 to 10 ticks: the cuts are the 4th and 7th.
    volatility = found["5"]
    assert {name: c.entry.summary.n for name, c in volatility.items()} == {
        "낮음": 4,
        "중간": 3,
        "높음": 3,
        NO_SIGMA: 1,
    }
    low = volatility["낮음"]
    assert confusion(low) == {"up": [1, 1, 0], "flat": [0, 1, 0], "down": [0, 0, 1]}
    assert low.lift.value == pytest.approx(1 / 4)
    assert low.by_day is None  # one day


def test_the_whole_period_agrees_with_the_leaderboard(db: Path) -> None:
    [whole] = cells(db)["1"].values()
    with read_only(db) as conn:
        assert conn is not None
        conn.row_factory = sqlite3.Row
        rows = scored_rows(conn, [D1, D2], None, False)
    names = {b.name for b in baselines_for(TARGET_VERSION)}
    algorithms, baselines = split(
        scored_by_algorithm(on_target(rows, names | set(NAMES), TARGET_VERSION)), names
    )
    board = leaderboard(algorithms, baselines, MIN_SAMPLE, 0.00015)
    [turing] = [e for e in board.ranked + board.unranked if e.algorithm == "turing"]
    assert turing.summary.n == whole.entry.summary.n
    assert turing.lifts[CLIMATE].lift == whole.lift
    assert turing.lifts[CLIMATE].by_day == whole.by_day


def test_the_text_is_exploratory_flags_small_cells_and_has_no_price(db: Path) -> None:
    text = error_breakdown(db, SETTINGS, D2, 20)
    lines = text.splitlines()
    assert lines[0] == "turing 오류 분석 2026-10-12 – 2026-10-13 (2일, 예측이 있는 날)"
    assert (
        lines[1].startswith("탐색용이다. 여기서 고른 칸을 그대로 판정에 쓰지 않는다.")
        and "홀드아웃" in lines[1]
    )
    assert "  전체: 채점 11 · 기후와 짝 10 · 2일 · 정답률 63.6% [" in text
    assert "lift +40.0%p (짝 하한 " in text
    assert "    상승 예측 6: 정답 상승 4 · 보합 1 · 하락 1 · 정밀도 66.7% [" in text
    assert "    정답: 상승 5 (45.5%) · 보합 3 (27.3%) · 하락 3 (27.3%)" in text
    assert "  09:30: 채점 5 · 기후와 짝 4 · 2일 · 표본 부족" in lines
    assert f"  {WIDER}: 채점 3 · 기후와 짝 3 · 2일 · 표본 부족" in lines
    assert f"  {NO_SIGMA}: 채점 1 · 기후와 짝 1 · 1일 · 표본 부족" in lines
    for price in ("81,250", "81250", "81,150", "81,350", "81,300", "81,200"):
        assert price not in text


def test_the_breakdown_reads_without_writing(db: Path, tmp_path: Path) -> None:
    files = sorted(p.name for p in tmp_path.iterdir())
    stored = db.read_bytes()
    error_breakdown(db, SETTINGS, D2, 20)
    assert sorted(p.name for p in tmp_path.iterdir()) == files  # no -wal or -shm made
    assert db.read_bytes() == stored
    assert error_breakdown(tmp_path / "none.sqlite3", SETTINGS, D2, 20) == "turing 오류 분석: 데이터 없음"
    assert "견주지 않는다" in error_breakdown(db, SETTINGS, D2, 20, CLIMATE)  # no lift over itself


# The scheduled job


def night(day: date, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), COMMIT, record, {}, Facts())


def test_the_job_runs_once_on_the_first_night_and_posts_to_431() -> None:
    assert ERROR_BREAKDOWN.name == "error-breakdown-431"
    assert ERROR_BREAKDOWN.command == ("error-breakdown", "--days", "20", "--algorithm", "turing")
    assert ERROR_BREAKDOWN.timeout == timedelta(hours=1)
    assert [t.issue for t in ERROR_BREAKDOWN.targets] == [431]
    assert ERROR_BREAKDOWN.label == ("ready-for-agent",)
    note = ERROR_BREAKDOWN.targets[0].note
    assert "후속 티켓" in note and "홀드아웃" in note and "표본 부족" in note
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(ERROR_BREAKDOWN) < first, "before the simulations and pre-validations"
    assert ERROR_BREAKDOWN.due(night(FRIDAY))
    assert ERROR_BREAKDOWN.due(night(SATURDAY))  # it reads the stored days: any night
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 30.0)
    assert not ERROR_BREAKDOWN.due(night(MONDAY, ran))
    assert ERROR_BREAKDOWN.due(night(MONDAY, replace(ran, outcome=INTERRUPTED)))


async def test_the_job_runs_the_real_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    state = JobState.load(tmp_path / STATE_FILE)
    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [ERROR_BREAKDOWN],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    [body] = hub.bodies(431)
    assert "turing 오류 분석: 데이터 없음" in body and "후속 티켓" in body
    assert state.jobs["error-breakdown-431"].exit_code == 0
