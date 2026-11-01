"""turing's pre-registered hypotheses judged on a fixed holdout (#447, ``krx-quant error-holdout``).

Input: stores holding hand-made predictions of turing and the climate baseline, their bar closes'
touch answers and the barrier's σ̂₄ there, on days of #431's discovery period and after it, with
predictions the leaderboard leaves out (a special one, one by a model of another target). Output:
the fixed window (the first ten days after 2026-10-30, none of the discovery period's), σ̂₄'s cuts
(from the discovery period only), the verdicts (a positive hypothesis on the lower end of the
interval by trading day, a negative one on its upper end), the text, and the scheduled job.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.barriers import BARRIER
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.climate import CLIMATE
from krx_quant.clock import FakeClock
from krx_quant.feeds.base import kst_day
from krx_quant.github import GitHub
from krx_quant.holdout import (
    AFTER,
    DAYS,
    DISCOVERY,
    FAILS,
    HYPOTHESES,
    LEVEL,
    MIN_SCORED,
    STANDS,
    TARGET,
    TOO_FEW,
    Holdout,
    days_in_window,
    error_holdout,
    judge,
    read_holdout,
    verdict,
    window_days,
)
from krx_quant.kinds import Answer
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score, read_only
from krx_quant.pricing import Direction
from krx_quant.scheduled import (
    ERROR_BREAKDOWN,
    ERROR_HOLDOUT,
    FAILED,
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
from krx_quant.stats import Lift, Scored, t_quantile
from tests.fake_github import REPO, TOKEN, fake_github
from tests.test_leaderboard_api import touch
from tests.test_scheduled import AT, CALENDAR, COMMIT, FRIDAY, MONDAY, SATURDAY, Told, at

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
SYMBOL, OTHER = "005930", "000660"
TICK, ANCHOR = 100, 81_250
# The discovery period's last two trading days, and the trading days after it.
OCT29, OCT30 = date(2026, 10, 29), date(2026, 10, 30)
AFTER_DAYS = [
    date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17)
]  # Monday 2 November on, no weekend


@dataclass(frozen=True)
class Bar:
    """A bar close: turing's call, the climate's (None: it made none), the touch answer and σ̂₄
    in ticks (None: w = k)."""

    day: date
    hhmm: str
    turing: Direction
    climate: Direction | None
    answer: Direction
    sigma: float | None = None
    symbol: str = SYMBOL
    version: str = "v1"  # turing's model: "old" was trained on another target
    special: bool = False


def stored(db: Path, bars: Sequence[Bar]) -> None:
    """``bars`` in the store at ``db``, each prediction scored once its four minutes are over."""
    store = PredictionStore(db)
    store.record_models([("turing", "v1", TARGET), ("turing", "old", 2)])
    made: list[tuple[NewPrediction, Direction]] = []
    for b in bars:
        t = at(b.day, b.hhmm)
        calls = [("turing", b.version, b.turing)]
        if b.climate is not None:
            calls.append((CLIMATE, "baseline-climate-default", b.climate))
        for algorithm, version, call in calls:
            p = NewPrediction(
                b.symbol,
                algorithm,
                version,
                t,
                t,
                SURE[call],
                t + timedelta(minutes=4),
                ANCHOR,
                ANCHOR,
                2,
                special=b.special,
            )
            made.append((p, b.answer))
    pending = store.add_all([p for p, _ in made]).pending
    assert len(pending) == len(made)
    store.score_all(
        [(q.prediction_id, Score(ANCHOR, a, a, None)) for q, (_, a) in zip(pending, made, strict=True)],
        max(p.target_at for p, _ in made),
    )
    store.add_touches([touch(b.symbol, at(b.day, b.hhmm), b.answer) for b in bars])
    sigmas = [b for b in bars if b.sigma is not None]
    if sigmas:
        store.add_kind_answers(BARRIER.name, BARRIER.answer_version, [answer(b) for b in sigmas])
    store.close()


def answer(b: Bar) -> tuple[str, datetime, Answer, bool]:
    path = {"w": 2, "sigma": b.sigma, "k": 2, "tick": TICK, "anchor": ANCHOR}
    return (b.symbol, at(b.day, b.hhmm), Answer("timeout", None, path), False)


def sigma_answers(db: Path, sigmas: Sequence[tuple[str, date, str, float]]) -> None:
    """The barrier's σ̂₄ (ticks) alone at (symbol, day, bar close)s: no prediction there."""
    store = PredictionStore(db)
    store.add_kind_answers(
        BARRIER.name,
        BARRIER.answer_version,
        [answer(Bar(day, hhmm, UP, None, UP, sigma, symbol)) for symbol, day, hhmm, sigma in sigmas],
    )
    store.close()


def bp(ticks: float) -> float:
    """σ̂₄ in basis points of the mid-point, as ``read_sigmas`` converts it."""
    return ticks * TICK * 1e4 / ANCHOR


def holdout(db: Path) -> Holdout:
    with read_only(db) as conn:
        assert conn is not None
        return read_holdout(conn)


# What was fixed before the window


def test_the_pre_registered_settings_are_the_issue_s() -> None:
    assert (DISCOVERY, AFTER, DAYS, MIN_SCORED, TARGET) == ((date(2026, 10, 12), OCT30), OCT30, 10, 200, 3)
    assert LEVEL == pytest.approx(1 - 0.05 / len(HYPOTHESES)) and len(HYPOTHESES) == 4
    assert [(h.name, h.sign) for h in HYPOTHESES] == [("H1", -1), ("H2", 1), ("H3", 1), ("H4", -1)]


# The window


def test_the_fixed_window_is_the_first_ten_days_after_the_discovery_period_and_never_mixes_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / DB_NAME
    discovery = [Bar(day, "09:01", UP, FLAT, UP, 1.0) for day in (OCT29, OCT30)]
    # On 2 November, beside the climate's, only turing's predictions the leaderboard leaves out: one
    # by a model of another target, one in a special window.
    left_out = [
        Bar(AFTER_DAYS[0], "09:01", UP, FLAT, UP, version="old"),
        Bar(AFTER_DAYS[0], "09:02", UP, FLAT, UP, special=True),
    ]
    filling = [Bar(day, "09:01", UP, FLAT, UP) for day in AFTER_DAYS[1:10]]  # nine days
    stored(db, [*discovery, *left_out, *filling])
    with read_only(db) as conn:
        assert conn is not None
        assert window_days(conn) == AFTER_DAYS[1:10], "filling: nine of ten"
    assert days_in_window(db) == 9
    data = holdout(db)
    assert not data.full
    assert "창이 아직 차지 않았다" in error_holdout(db) and "H1 " not in error_holdout(db)

    stored(db, [Bar(day, "09:01", UP, FLAT, UP) for day in AFTER_DAYS[10:]])  # two days more
    data = holdout(db)
    assert data.full and data.days == AFTER_DAYS[1:11], "the first ten: the twelfth day is not in it"
    assert days_in_window(db) == DAYS
    assert all(day > OCT30 for day in data.days)
    assert {kst_day(r.bar_close) for r in data.records} == set(data.days), "none of the discovery period's"
    assert {kst_day(r.bar_close) for r in data.climate} == set(data.days)
    assert all(r.algorithm == "turing" and r.model_version == "v1" for r in data.records)


def test_the_window_s_count_is_zero_without_a_readable_db(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert days_in_window(tmp_path / "none.sqlite3") == 0
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    with caplog.at_level(logging.ERROR):
        assert days_in_window(empty) == 0  # no table: logged, not raised
    assert "holdout window" in caplog.text


# σ̂₄'s cuts


def test_the_sigma_cuts_come_from_the_discovery_period_s_values_only(tmp_path: Path) -> None:
    db = tmp_path / DB_NAME
    first, last = DISCOVERY
    # The discovery period, both ends in: σ̂₄ 1 to 9 ticks. The cuts are the 3rd and the 6th.
    inside = [(SYMBOL, first if i <= 4 else last, f"09:{i:02d}", float(i)) for i in range(1, 10)]
    # Before it and after it, values that would move the cuts if they were read; and a symbol with
    # values after it only.
    outside = [
        (SYMBOL, first - timedelta(days=3), "09:01", 1000.0),
        (SYMBOL, last + timedelta(days=1), "09:01", 0.1),
        (OTHER, last + timedelta(days=1), "09:02", 7.0),
    ]
    sigma_answers(db, [*inside, *outside])
    rows = (
        ("10:01", 2.0, SYMBOL),
        ("10:02", 4.0, SYMBOL),
        ("10:03", 7.0, SYMBOL),
        ("10:04", 300.0, SYMBOL),
        ("10:05", 400.0, SYMBOL),
        ("10:06", None, SYMBOL),
        ("10:07", 7.0, OTHER),
    )
    window = [
        Bar(day, hhmm, UP, FLAT, UP, s, symbol) for day in AFTER_DAYS[:DAYS] for hhmm, s, symbol in rows
    ]
    stored(db, window)
    data = holdout(db)
    assert data.full
    assert data.cuts == {SYMBOL: [bp(3), bp(6)]}, "the window's σ̂₄ (2 to 400 ticks) moved nothing"
    terciles = {(r.symbol, r.bar_close.strftime("%H:%M")): data.tercile(r) for r in data.records}
    assert terciles == {
        (SYMBOL, "10:01"): "low",
        (SYMBOL, "10:02"): "mid",
        (SYMBOL, "10:03"): "high",  # by the window's own cuts (4 and 300 ticks), 7 would be mid
        (SYMBOL, "10:04"): "high",  # and 300 too
        (SYMBOL, "10:05"): "high",
        (SYMBOL, "10:06"): None,  # no σ̂₄
        (OTHER, "10:07"): None,  # no cuts for the symbol
    }
    first_day = {
        r.bar_close.strftime("%H:%M"): r for r in data.records if kst_day(r.bar_close) == data.days[0]
    }
    quiet, moving = HYPOTHESES[:2]
    assert [quiet.selects(first_day[h], data) for h in ("10:01", "10:02", "10:03")] == [True, False, False]
    assert [moving.selects(first_day[h], data) for h in ("10:01", "10:02", "10:03")] == [False, False, True]
    text = error_holdout(db)
    assert f"{bp(3):.1f}" not in text and f"{bp(6):.1f}" not in text, "no cut is printed"


# The verdict


def by_day(low: float, high: float, days: int = DAYS) -> Lift:
    return Lift(days, (low + high) / 2, low, high, None)


def test_a_positive_hypothesis_stands_only_on_the_lower_end_and_a_negative_one_on_the_upper() -> None:
    enough = MIN_SCORED
    assert verdict(enough, by_day(0.001, 0.2), 1) == STANDS
    assert verdict(enough, by_day(-0.001, 0.2), 1) == FAILS, "a positive lift whose lower end is below 0"
    assert verdict(enough, by_day(0.0, 0.2), 1) == FAILS, "a lower end of 0 is not above it"
    assert verdict(enough, by_day(-0.3, -0.1), 1) == FAILS, (
        "a negative lift does not stand for a positive claim"
    )
    assert verdict(enough, by_day(-0.2, -0.001), -1) == STANDS
    assert verdict(enough, by_day(-0.2, 0.001), -1) == FAILS, "a negative lift whose upper end is above 0"
    assert verdict(enough, by_day(-0.2, 0.0), -1) == FAILS
    assert verdict(enough, by_day(0.1, 0.3), -1) == FAILS, (
        "a positive lift does not stand for a negative claim"
    )
    # Too few: not judged, however clear.
    assert verdict(enough - 1, by_day(0.1, 0.3), 1) == TOO_FEW, "199 scored"
    assert verdict(enough, by_day(0.1, 0.3, DAYS - 1), 1) == TOO_FEW, "pairs on nine days"
    assert verdict(enough, None, 1) == TOO_FEW


def scored(algorithm: str, day: date, minute: int, correct: bool) -> Scored:
    """A sure call at 09:``minute`` on ``day`` of an up answer: up if ``correct``, else flat."""
    p = (1.0, 0.0, 0.0) if correct else (0.0, 1.0, 0.0)
    return Scored(algorithm, "v1", SYMBOL, at(day, f"09:{minute:02d}"), *p, UP)


def cell(gains: Sequence[Sequence[int]]) -> tuple[list[Scored], list[Scored]]:
    """turing's and the climate's records of a cell whose day i gains ``gains[i]`` (+1, 0 or −1)."""
    ours: list[Scored] = []
    theirs: list[Scored] = []
    for day, day_gains in zip(AFTER_DAYS, gains, strict=False):
        for minute, gain in enumerate(day_gains, start=1):
            ours.append(scored("turing", day, minute, gain >= 0))
            theirs.append(scored(CLIMATE, day, minute, gain <= 0))
    return ours, theirs


def test_judge_takes_the_interval_by_trading_day_at_98_75_percent() -> None:
    # Ten days of 21 pairs: day i gains +1 on 10 + i of them, 0 on the rest.
    gains = [[1] * (10 + i) + [0] * (11 - i) for i in range(DAYS)]
    ours, theirs = cell(gains)
    j = judge(ours, theirs, 1)
    means = [(10 + i) / 21 for i in range(DAYS)]
    mean = sum(means) / DAYS
    sd = (sum((m - mean) ** 2 for m in means) / (DAYS - 1)) ** 0.5
    half = t_quantile(1 - (1 - LEVEL) / 2, DAYS - 1) * sd / DAYS**0.5
    assert (j.scored, j.lift.n, j.days) == (210, 210, DAYS)
    assert j.by_day is not None and j.by_day.n == DAYS
    assert j.by_day.value == pytest.approx(mean)
    assert j.by_day.low == pytest.approx(mean - half) and j.by_day.high == pytest.approx(mean + half)
    assert j.verdict == STANDS
    assert judge(ours, theirs, -1).verdict == FAILS
    at_95 = judge(ours, theirs, 1, level=0.95).lift
    assert at_95.low is not None and j.lift.low is not None and j.lift.low < at_95.low, "98.75% is wider"
    assert judge(ours[:-11], theirs, 1).verdict == TOO_FEW, "199 scored"
    assert judge(*cell(gains[: DAYS - 1]), 1).verdict == TOO_FEW, "nine days"


# The text


def window_of_two_cells() -> list[Bar]:
    """Ten days of 21 early bar closes turing calls right and the climate wrong (on day i, i % 3 of
    them both right), 21 at lunch the other way round, and three later ones with σ̂₄: H3 and H4
    stand, H1 and H2 have too few."""
    bars: list[Bar] = []
    for i, day in enumerate(AFTER_DAYS[:DAYS]):
        for m in range(1, 22):
            both = m <= i % 3
            bars.append(Bar(day, f"09:{m:02d}", UP, UP if both else FLAT, UP))
            bars.append(Bar(day, f"12:{m:02d}", FLAT if both else UP, FLAT, FLAT))
        for hhmm, sigma in (("13:01", 1.0), ("13:02", 5.0), ("13:03", 9.0)):
            bars.append(Bar(day, hhmm, UP, FLAT, UP, sigma))
    return bars


def test_the_text_judges_each_hypothesis_on_the_full_window_without_prices(tmp_path: Path) -> None:
    db = tmp_path / DB_NAME
    first, last = DISCOVERY
    sigma_answers(db, [(SYMBOL, first, f"09:{i:02d}", float(i)) for i in range(1, 10)])
    stored(db, window_of_two_cells())
    text = error_holdout(db)
    lines = text.splitlines()
    assert lines[0] == "turing 홀드아웃 판정 (#447, 사전 등록 가설)"
    assert "2026-11-02 – 2026-11-13, 10/10일." in lines[1] and "고정 창이다" in lines[1]
    assert "98.75% t 구간(가설 넷의 본페로니)" in text
    assert "발견 기간 2026-10-12 – 2026-10-30" in text
    [h1, h2, h3, h4] = [line for line in lines if line.startswith(("H1 ", "H2 ", "H3 ", "H4 "))]
    assert h1 == "H1 조용할 때 진다(σ̂₄ 하위 삼분위, lift < 0): 채점 10 · 기후와 짝 10 · 10일 · 표본 부족"
    assert h2 == "H2 움직일 때 이긴다(σ̂₄ 상위 삼분위, lift > 0): 채점 10 · 기후와 짝 10 · 10일 · 표본 부족"
    assert h3.startswith(
        "H3 장 초반에 이긴다(09:00·09:30·10:00·10:30 칸, lift > 0): "
        "채점 210 · 기후와 짝 210 · 10일 · 정답률 100.0%"
    )
    assert h3.endswith(" · 섰다") and "거래일 구간 [+" in h3
    assert h4.startswith("H4 점심에 진다(12:00·12:30 칸, lift < 0): 채점 210 · 기후와 짝 210 · 10일")
    assert h4.endswith(" · 섰다") and "lift -" in h4
    assert lines[-1] == "섰다: H3, H4 · 서지 않음: 없음 · 표본 부족: H1, H2"
    for price in ("81,250", "81250", "81,200", "81,300", "81,450", "81,050"):
        assert price not in text


def test_the_text_says_when_there_is_no_db(tmp_path: Path) -> None:
    assert (
        error_holdout(tmp_path / "none.sqlite3") == "turing 홀드아웃 판정 (#447, 사전 등록 가설): 데이터 없음"
    )


def test_the_command_prints_it_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / DB_NAME
    stored(db, [Bar(day, "09:01", UP, FLAT, UP) for day in AFTER_DAYS[:3]])
    before = (sorted(p.name for p in db.parent.iterdir()), db.read_bytes())
    assert main(["error-holdout"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-02 – 2026-11-04, 3/10일." in out and "판정하지 않는다" in out
    assert (sorted(p.name for p in db.parent.iterdir()), db.read_bytes()) == before


# The scheduled job


def night(day: date, days: int, record: JobRecord | None = None, commit: str = COMMIT) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), commit, record, {}, Facts(holdout_days=days))


def test_the_job_runs_once_on_the_first_night_the_window_is_full_and_posts_to_447() -> None:
    job = ERROR_HOLDOUT
    assert (job.name, job.command, job.timeout) == (
        "error-holdout-447",
        ("error-holdout",),
        timedelta(hours=1),
    )
    assert [t.issue for t in job.targets] == [447] and job.label == ("ready-for-agent",)
    note = job.targets[0].note
    assert "`섰다`인 가설만" in note and "바꾸지 않는다" in note and "#448" in note and "#449" in note
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(ERROR_BREAKDOWN) < JOBS.index(job) < first, "before the simulations and pre-validations"
    assert not job.due(night(FRIDAY, DAYS - 1)), "the window is not full"
    assert job.due(night(FRIDAY, DAYS)) and job.due(night(SATURDAY, DAYS)), "trading or not"
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 30.0)
    assert not job.due(night(MONDAY, DAYS, ran)), "once"
    assert job.due(night(MONDAY, DAYS, replace(ran, outcome=INTERRUPTED))), "again after a restart cut it"
    failed = replace(ran, outcome=FAILED, exit_code=1)
    assert not job.due(night(MONDAY, DAYS, failed)), "a failure is not run again on the same code"
    assert job.due(night(MONDAY, DAYS, failed, "fedcba")), "but again once a deploy brings other code"


async def test_the_job_runs_the_real_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    state = JobState.load(tmp_path / STATE_FILE)

    async def facts() -> Facts:
        return Facts(holdout_days=DAYS)

    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [ERROR_HOLDOUT],
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
            facts=facts,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    [body] = hub.bodies(447)
    assert "turing 홀드아웃 판정 (#447, 사전 등록 가설): 데이터 없음" in body and "`섰다`인 가설만" in body
    assert state.jobs["error-holdout-447"].exit_code == 0


async def test_the_nightly_facts_count_the_window_s_days_in_the_engine_db(tmp_path: Path) -> None:
    from krx_quant.scheduled import read_facts
    from tests.test_simulation_daily import CALENDAR as DAILY_CALENDAR
    from tests.test_simulation_daily import NIGHT, engine_on
    from tests.test_simulation_daily import settings as daily_settings

    s = daily_settings(tmp_path, signal=0.0, days=30)
    clock = FakeClock(NIGHT)
    stored(s.state_dir / DB_NAME, [Bar(day, "09:01", UP, FLAT, UP) for day in AFTER_DAYS[:4]])
    e = engine_on(s, clock, [])
    try:
        facts = await read_facts(s, DAILY_CALENDAR, clock, e)
    finally:
        e.store.close()
    assert facts.holdout_days == 4
