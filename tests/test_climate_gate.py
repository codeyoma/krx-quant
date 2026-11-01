"""A regime gate judged on #447's holdout (#449, ``krx-quant climate-gate``).

Input: hand-made records of turing and the climate baseline at bar closes of every kind the gate
tells apart (σ̂₄'s terciles, none, a symbol without cuts, the slots on both sides of lunch's), and
stores holding them with σ̂₄ in and around #431's discovery period. Output: the arm the gate takes
at each bar close, the three arms on the same bar closes, σ̂₄'s cuts (from the discovery period
only), the verdict (the gate's lift over turing by trading day, at 95%, on its lower end), the
text, and the scheduled job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.cli import main
from krx_quant.climate import CLIMATE
from krx_quant.climate_gate import GATE, LEVEL, arms, climate_gate, compare, gated
from krx_quant.clock import FakeClock
from krx_quant.feeds.base import kst_day
from krx_quant.github import GitHub
from krx_quant.holdout import DAYS, DISCOVERY, FAILS, MIN_SCORED, STANDS, TOO_FEW, Holdout
from krx_quant.predictions import DB_NAME, iso
from krx_quant.pricing import Direction
from krx_quant.scheduled import (
    CLIMATE_GATE,
    ERROR_HOLDOUT,
    FAILED,
    INTERRUPTED,
    JOBS,
    OK,
    STATE_FILE,
    JobRecord,
    JobState,
    Scheduler,
)
from krx_quant.stats import Scored, t_quantile
from tests.fake_github import REPO, TOKEN, fake_github
from tests.test_holdout import AFTER_DAYS, OTHER, SYMBOL, Bar, bp, holdout, night, sigma_answers, stored
from tests.test_scheduled import AT, CALENDAR, COMMIT, FRIDAY, MONDAY, SATURDAY, Told, at

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
P = {UP: (1.0, 0.0, 0.0), FLAT: (0.0, 1.0, 0.0), DOWN: (0.0, 0.0, 1.0)}
CUTS = [3.0, 6.0]  # basis points: low up to 3, mid up to 6, high above


def record(
    algorithm: str, day: date, hhmm: str, call: Direction, answer: Direction, symbol: str = SYMBOL
) -> Scored:
    return Scored(algorithm, "v1", symbol, at(day, hhmm), *P[call], answer)


def window(
    rows: Sequence[tuple[date, str, Direction | None, Direction | None, Direction, float | None, str]],
) -> Holdout:
    """A full window of (day, bar close, turing's call or None, the climate's or None, the answer,
    σ̂₄ in basis points or None, symbol), the cuts ``CUTS`` for ``SYMBOL`` alone."""
    records = [record("turing", d, h, t, a, s) for d, h, t, _, a, _, s in rows if t is not None]
    climate = [record(CLIMATE, d, h, c, a, s) for d, h, _, c, a, _, s in rows if c is not None]
    sigmas = {(s, iso(at(d, h))): sigma for d, h, _, _, _, sigma, s in rows if sigma is not None}
    return Holdout(AFTER_DAYS[:DAYS], records, climate, sigmas, {SYMBOL: CUTS})


# The rule


def test_the_gate_takes_the_climate_when_quiet_or_at_lunch_and_turing_elsewhere() -> None:
    day = AFTER_DAYS[0]
    # turing calls up and the climate flat everywhere: the gate's call tells whose it took.
    rows = [
        ("10:01", 2.0, SYMBOL),  # low: the climate
        ("10:02", 3.0, SYMBOL),  # on the lower cut: low, the climate
        ("10:03", 4.0, SYMBOL),  # mid: turing
        ("10:04", 7.0, SYMBOL),  # high: turing
        ("10:05", None, SYMBOL),  # no σ̂₄, not at lunch: turing
        ("11:59", 7.0, SYMBOL),  # the 11:30 slot: turing
        ("12:00", 7.0, SYMBOL),  # the 11:30 slot's last bar close (consensus.slot): turing
        ("12:01", 7.0, SYMBOL),  # the 12:00 slot, high: the climate
        ("12:31", None, SYMBOL),  # the 12:30 slot, no σ̂₄: the climate
        ("13:00", 1.0, SYMBOL),  # the 12:30 slot's last bar close, low too: the climate
        ("13:01", 7.0, SYMBOL),  # the 13:00 slot: turing
        ("13:02", 1.0, SYMBOL),  # low, after lunch: the climate
        ("10:01", 1.0, OTHER),  # a symbol without cuts, not at lunch: turing
        ("12:05", 1.0, OTHER),  # a symbol without cuts, at lunch: the climate
    ]
    data = window([(day, h, UP, FLAT, UP, s, symbol) for h, s, symbol in rows])
    takes = {(r.symbol, r.bar_close.strftime("%H:%M")): gated(r, data) for r in data.records}
    assert takes == {
        (SYMBOL, "10:01"): True,
        (SYMBOL, "10:02"): True,
        (SYMBOL, "10:03"): False,
        (SYMBOL, "10:04"): False,
        (SYMBOL, "10:05"): False,
        (SYMBOL, "11:59"): False,
        (SYMBOL, "12:00"): False,
        (SYMBOL, "12:01"): True,
        (SYMBOL, "12:31"): True,
        (SYMBOL, "13:00"): True,
        (SYMBOL, "13:01"): False,
        (SYMBOL, "13:02"): True,
        (OTHER, "10:01"): False,
        (OTHER, "12:05"): True,
    }
    a = arms(data)
    calls = {(g.symbol, g.bar_close.strftime("%H:%M")): g.predicted for g in a.gate}
    assert calls == {k: FLAT if took else UP for k, took in takes.items()}, "the climate's call, or turing's"
    assert {g.algorithm for g in a.gate} == {GATE}
    assert (a.handed, a.quiet, a.lunch, a.unsorted) == (7, 4, 4, 4)  # 13:00 is both quiet and at lunch


def test_the_three_arms_are_on_the_same_bar_closes() -> None:
    day = AFTER_DAYS[0]
    data = window(
        [
            (day, "10:01", UP, FLAT, UP, 2.0, SYMBOL),
            (day, "10:02", UP, FLAT, UP, 7.0, SYMBOL),
            (day, "10:03", UP, None, UP, 2.0, SYMBOL),  # the climate made none: in no arm
            (day, "10:04", None, FLAT, UP, 7.0, SYMBOL),  # turing made none: in no arm
            (day + timedelta(days=1), "10:01", UP, FLAT, UP, 7.0, SYMBOL),
        ]
    )
    a = arms(data)
    keys = [[(r.symbol, r.bar_close) for r in arm] for arm in (a.gate, a.turing, a.climate)]
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) == 3
    assert {r.bar_close.strftime("%H:%M") for r in a.gate} == {"10:01", "10:02"}
    assert (a.handed, a.quiet, a.lunch) == (1, 1, 0)


# σ̂₄'s cuts


def test_the_gate_cuts_sigma_by_the_discovery_period_s_values_only(tmp_path: Path) -> None:
    db = tmp_path / DB_NAME
    first, last = DISCOVERY
    # The discovery period, both ends in: σ̂₄ 1 to 9 ticks; the cuts are the 3rd and the 6th.
    inside = [(SYMBOL, first if i <= 4 else last, f"09:{i:02d}", float(i)) for i in range(1, 10)]
    # Before it and just after it, values that would bring the cuts down to 1 and 5 if they were read.
    outside = [
        *((SYMBOL, first - timedelta(days=3), f"09:0{i}", i / 10) for i in (1, 2)),
        (SYMBOL, last + timedelta(days=1), "09:01", 0.3),
    ]
    sigma_answers(db, [*inside, *outside])
    # The window's own values would cut at 4 and 300: 4 ticks would be low.
    rows = (("10:01", 2.0), ("10:02", 4.0), ("10:03", 5.0), ("10:04", 300.0), ("10:05", 400.0))
    stored(db, [Bar(day, hhmm, UP, FLAT, UP, sigma) for day in AFTER_DAYS[:DAYS] for hhmm, sigma in rows])
    text = climate_gate(db)
    data = holdout(db)
    assert data.full and data.cuts == {SYMBOL: [bp(3), bp(6)]}
    calls = {(kst_day(g.bar_close), g.bar_close.strftime("%H:%M")): g.predicted for g in arms(data).gate}
    for day in data.days:
        assert [calls[(day, h)] for h, _ in rows] == [FLAT, UP, UP, UP, UP], "only 2 ticks is quiet"
    assert f"{bp(3):.1f}" not in text and f"{bp(6):.1f}" not in text, "no cut is printed"


# The verdict


def lunch_and_morning(gains: Sequence[int]) -> Holdout:
    """Ten days of 21 lunch bar closes and 21 morning ones (no σ̂₄, so turing's): on day i, the
    gate gains ``gains[i]`` over turing at lunch (the climate right and turing wrong on that many,
    or turing right and the climate wrong on as many as its negative), both right on the others;
    in the morning turing right and the climate wrong."""
    rows = []
    for day, gain in zip(AFTER_DAYS[:DAYS], gains, strict=True):
        for m in range(1, 22):
            t, c = (FLAT, UP) if m <= gain else (UP, FLAT) if m <= -gain else (UP, UP)
            rows.append((day, f"12:{m:02d}", t, c, UP, None, SYMBOL))
            rows.append((day, f"10:{m:02d}", UP, FLAT, UP, None, SYMBOL))
    return window(rows)


def test_the_gate_is_better_than_turing_only_on_the_lower_end_of_its_lift_by_day_at_95_percent() -> None:
    gains = [5 + i for i in range(DAYS)]
    c = compare(lunch_and_morning(gains))
    means = [g / 42 for g in gains]
    mean = sum(means) / DAYS
    sd = (sum((m - mean) ** 2 for m in means) / (DAYS - 1)) ** 0.5
    half = t_quantile(1 - (1 - LEVEL) / 2, DAYS - 1) * sd / DAYS**0.5
    assert LEVEL == 0.95, "one claim: no Bonferroni"
    o = c.over_turing
    assert (o.scored, o.lift.n, o.days) == (420, 420, DAYS)
    assert o.by_day is not None and o.by_day.value == pytest.approx(mean)
    assert o.by_day.low == pytest.approx(mean - half) and o.by_day.high == pytest.approx(mean + half)
    assert o.verdict == STANDS
    assert o.accuracy == pytest.approx(1.0) and o.baseline == pytest.approx(1 - sum(gains) / (DAYS * 42))
    v = c.over_climate
    assert v.baseline == pytest.approx(0.5) and v.lift.value == pytest.approx(0.5), "the morning's"
    assert (c.arms.handed, c.arms.lunch, c.arms.quiet, c.arms.unsorted) == (210, 210, 0, 420)

    # A gain whose lower end is at or below 0, however the days lean: not better.
    leaning = compare(lunch_and_morning([3, -2] * (DAYS // 2))).over_turing
    assert leaning.lift.value is not None and leaning.lift.value > 0
    assert leaning.by_day is not None and leaning.by_day.low is not None and leaning.by_day.low <= 0
    assert leaning.verdict == FAILS
    worse = compare(lunch_and_morning([-(1 + i % 3) for i in range(DAYS)])).over_turing
    assert worse.verdict == FAILS, "a gate worse than turing is not better"
    assert compare(lunch_and_morning([0] * DAYS)).over_turing.verdict == FAILS, "no gain at all"


def test_too_few_is_not_judged() -> None:
    data = lunch_and_morning([5 + i for i in range(DAYS)])
    nine = replace(data, records=[r for r in data.records if kst_day(r.bar_close) != AFTER_DAYS[DAYS - 1]])
    assert compare(nine).over_turing.verdict == TOO_FEW, "pairs on nine days"
    by_day: dict[date, list[Scored]] = {}
    for r in data.records:
        by_day.setdefault(kst_day(r.bar_close), []).append(r)
    enough = [r for records in by_day.values() for r in records[: MIN_SCORED // DAYS]]
    assert compare(replace(data, records=enough)).over_turing.verdict == STANDS, "200 bar closes on ten days"
    assert compare(replace(data, records=enough[:-1])).over_turing.verdict == TOO_FEW, "199"


# The text


def window_of_three_arms() -> list[Bar]:
    """Ten days of 21 quiet bar closes (σ̂₄ 2 ticks) and 21 at lunch (σ̂₄ 2 ticks: both) the climate
    calls right and turing wrong (at lunch on day i, i % 3 of them both right), 21 moving ones (9
    ticks) the other way round, and one without σ̂₄ both call right."""
    bars: list[Bar] = []
    for i, day in enumerate(AFTER_DAYS[:DAYS]):
        for m in range(1, 22):
            bars.append(Bar(day, f"10:{m:02d}", FLAT, UP, UP, 2.0))
            bars.append(Bar(day, f"12:{m:02d}", UP if m <= i % 3 else FLAT, UP, UP, 2.0))
            bars.append(Bar(day, f"13:{m:02d}", UP, FLAT, UP, 9.0))
        bars.append(Bar(day, "14:01", UP, UP, UP))
    return bars


def test_the_text_compares_the_three_arms_on_the_full_window_without_prices(tmp_path: Path) -> None:
    db = tmp_path / DB_NAME
    first, _ = DISCOVERY
    sigma_answers(db, [(SYMBOL, first, f"09:{i:02d}", float(i)) for i in range(1, 10)])
    stored(db, window_of_three_arms())
    text = climate_gate(db)
    lines = text.splitlines()
    assert lines[0] == "turing·기후 국면 문 홀드아웃 판정 (#449)"
    assert "2026-11-02 – 2026-11-13, 10/10일." in lines[1] and "#447과 같다" in lines[1]
    assert "σ̂₄ 하위 삼분위이거나 12:00·12:30 칸이면 baseline-climate" in text
    assert "발견 기간 2026-10-12 – 2026-10-30" in text and "95% t 구간" in text
    [handed] = [line for line in lines if line.startswith("기후로 넘긴 봉 마감")]
    assert handed == (
        "기후로 넘긴 봉 마감: 420 / 640 (65.6%) · σ̂₄ 하위 삼분위 420 · 12:00·12:30 칸 210 · 둘 다 210"
    )
    assert "같은 봉 마감 640개 중 10개는 σ̂₄나 종목의 경계가 없어 어느 삼분위에도 들지 않는다" in text
    [rates] = [line for line in lines if line.startswith("맞힘률")]
    assert rates == "맞힘률(세 분류, 같은 봉 마감 640개): 문 100.0% · turing 35.8% · baseline-climate 67.2%"
    [over_turing] = [line for line in lines if line.startswith("문 − turing")]
    assert over_turing.startswith("문 − turing: 짝 640 · 10일 · lift +64.2%p · 짝 구간 [+")
    assert "거래일 구간 [+" in over_turing and over_turing.endswith(" · 섰다")
    [over_climate] = [line for line in lines if line.startswith("문 − baseline-climate")]
    assert over_climate.startswith("문 − baseline-climate: 짝 640 · 10일 · lift +32.8%p")
    assert not over_climate.endswith(("섰다", "서지 않음", "표본 부족")), "not judged"
    assert lines[-1] == "문이 turing보다 낫다(turing 대비 거래일 95% 하한 > 0): 섰다"
    for price in ("81,250", "81250", "81,200", "81,300", "81,450", "81,050"):
        assert price not in text


def test_nothing_is_judged_before_the_window_is_full(tmp_path: Path) -> None:
    db = tmp_path / DB_NAME
    stored(db, [Bar(day, "12:01", UP, FLAT, UP) for day in AFTER_DAYS[: DAYS - 1]])
    text = climate_gate(db)
    assert "2026-11-02 – 2026-11-12, 9/10일." in text and "맞힘률" not in text
    assert text.splitlines()[-1] == "창이 아직 차지 않았다. 판정하지 않는다(10일이 차면 판정한다)."


def test_the_text_says_when_there_is_no_db(tmp_path: Path) -> None:
    assert climate_gate(tmp_path / "none.sqlite3") == "turing·기후 국면 문 홀드아웃 판정 (#449): 데이터 없음"


def test_the_command_prints_it_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    db = tmp_path / "state" / DB_NAME
    stored(db, [Bar(day, "12:01", UP, FLAT, UP) for day in AFTER_DAYS[:3]])
    before = (sorted(p.name for p in db.parent.iterdir()), db.read_bytes())
    assert main(["climate-gate"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-02 – 2026-11-04, 3/10일." in out and "판정하지 않는다" in out
    assert (sorted(p.name for p in db.parent.iterdir()), db.read_bytes()) == before


# The scheduled job


def test_the_job_runs_on_the_night_447_s_does_and_posts_to_449() -> None:
    job = CLIMATE_GATE
    assert (job.name, job.command, job.timeout) == ("climate-gate-449", ("climate-gate",), timedelta(hours=1))
    assert [t.issue for t in job.targets] == [449] and job.label == ("ready-for-agent",)
    note = job.targets[0].note
    assert "엔진에는 넣지 않는다" in note and "하한 > 0" in note and "#447" in note and "#448" in note
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(ERROR_HOLDOUT) < JOBS.index(job) < first, "beside #447's, before the simulations"
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 30.0)
    failed = replace(ran, outcome=FAILED, exit_code=1)
    nights = [
        night(FRIDAY, DAYS - 1),
        night(FRIDAY, DAYS),
        night(SATURDAY, DAYS),
        night(MONDAY, DAYS, ran),
        night(MONDAY, DAYS, replace(ran, outcome=INTERRUPTED)),
        night(MONDAY, DAYS, failed),
        night(MONDAY, DAYS, failed, "fedcba"),
    ]
    assert [job.due(n) for n in nights] == [False, True, True, False, True, False, True]
    assert [job.due(n) for n in nights] == [ERROR_HOLDOUT.due(n) for n in nights], "the same nights"


async def test_the_job_runs_the_real_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from krx_quant.scheduled import Facts

    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    state = JobState.load(tmp_path / STATE_FILE)

    async def facts() -> Facts:
        return Facts(holdout_days=DAYS)

    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [CLIMATE_GATE],
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
    [body] = hub.bodies(449)
    assert "turing·기후 국면 문 홀드아웃 판정 (#449): 데이터 없음" in body and "엔진에는 넣지 않는다" in body
    assert state.jobs["climate-gate-449"].exit_code == 0
