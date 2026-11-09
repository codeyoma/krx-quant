"""The SMT experiment (#523, ``krx-quant smt-experiment``): the frozen rule against the engine's final
consensus on the same bar closes, pooled into four claims, judged a trading day at a time.

Input: hand-made days (their events, answers and predictions) for the arithmetic; for the reading, a
data directory with recorded trades and backfilled bars and an engine DB with touch answers and the
final consensus's and the climate's predictions. Output: the pooling, the hit-rate difference, the
verdicts (by trading day, with too few not judged), the holdout's days (eligible whatever their
events), the backfilled days kept apart, the text without a price, and the two scheduled jobs.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant import smt_experiment
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.climate import CLIMATE
from krx_quant.consensus import FINAL
from krx_quant.engine import Event
from krx_quant.feeds.base import KST
from krx_quant.holdout import FAILS, STANDS, TOO_FEW
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.reference import BAR
from krx_quant.scheduled import (
    FAILED,
    INTERRUPTED,
    JOBS,
    NEYMAN_VARIANTS,
    OK,
    SMT_DISCOVERY,
    SMT_HOLDOUT,
    JobRecord,
    Night,
)
from krx_quant.smt import BACKFILLED, BEARISH, BULLISH, FUTURES, RECONSTRUCTED, RECORDED, SmtEvent
from krx_quant.smt_experiment import (
    AFTER,
    CALLS,
    CLAIMS,
    DAYS,
    LEVEL,
    MIN_DAYS,
    MIN_SCORED,
    RULE,
    Day,
    co_move,
    discovery_lines,
    holdout_days,
    holdout_lines,
    measure,
    measured,
    pair_units,
    units,
)
from krx_quant.stats import Scored, t_test
from tests.api_doubles import touch
from tests.replay_events import futures_trade, trade
from tests.test_smt import day_bars, record, store_backfill

A, B, F = "005930", "000660", FUTURES
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
SURE = {UP: Forecast(1, 0, 0, 0), FLAT: Forecast(0, 1, 0, 0), DOWN: Forecast(0, 0, 1, 0)}
# Trading days after the cutoff (2026-11-04), weekends left out.
HOLDOUT = [date(2026, 11, d) for d in (5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]
BEFORE = date(2026, 11, 3)  # a discovery day (a Tuesday)
BARS_ONLY = date(2026, 10, 30)  # a day with backfilled bars only (a Friday)
CALENDAR = MarketCalendar({d: True for d in [*HOLDOUT, AFTER, BEFORE, BARS_ONLY]})


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def event(
    day: date, hhmm: str, a: str, b: str, direction: str, minutes: int = 15, swept: str = ""
) -> SmtEvent:
    return SmtEvent(at(day, hhmm), a, b, direction, minutes, swept or a, RECONSTRUCTED)


def called(key: tuple[str, datetime], algorithm: str, call: Direction, answer: Direction) -> Scored:
    p = SURE[call]
    return Scored(algorithm, "v1", key[0], key[1], p.p_up, p.p_flat, p.p_down, answer)


# The arithmetic, on hand-made days


def test_each_claim_pools_its_pairs_and_stocks_once_and_never_the_futures() -> None:
    events = [
        event(BEFORE, "10:01", A, B, BEARISH),  # both stocks
        event(BEFORE, "10:01", A, F, BEARISH, swept=F),  # 005930 again, the futures swept: one unit
        event(BEFORE, "10:01", B, F, BEARISH, 30),  # another claim
        event(BEFORE, "11:01", B, F, BULLISH, swept=F),  # the stock gets the futures' sweep
    ]
    found = units(events)
    assert set(found) == set(CLAIMS)
    assert found[(BEARISH, 15)] == {(A, at(BEFORE, "10:01")), (B, at(BEFORE, "10:01"))}
    assert found[(BEARISH, 30)] == {(B, at(BEFORE, "10:01"))}
    assert found[(BULLISH, 15)] == {(B, at(BEFORE, "11:01"))}
    assert found[(BULLISH, 30)] == set()
    by_pair = pair_units(events)
    assert by_pair[((A, F), (BEARISH, 15))] == {(A, at(BEFORE, "10:01"))}
    assert by_pair[((A, B), (BEARISH, 15))] == found[(BEARISH, 15)]


def test_the_rule_is_scored_against_the_engine_on_the_same_bar_closes_only() -> None:
    a, b, later = (A, at(BEFORE, "10:01")), (B, at(BEFORE, "10:01")), (B, at(BEFORE, "11:01"))
    elsewhere = (A, at(BEFORE, "12:00"))  # no event: only in the day's shares
    answers = {a: DOWN, b: FLAT, later: UP, elsewhere: UP}
    engine = {a: called(a, FINAL, FLAT, DOWN), later: called(later, FINAL, DOWN, UP)}  # none at b
    climate = {k: called(k, CLIMATE, FLAT, v) for k, v in answers.items()}
    day = measure(
        BEFORE,
        RECORDED,
        [event(BEFORE, "10:01", A, B, BEARISH), event(BEFORE, "11:01", B, F, BULLISH, 30)],
        answers,
        engine,
        climate,
    )
    assert day.base == Counter({DOWN: 1, FLAT: 1, UP: 2})
    assert elsewhere not in day.answers and elsewhere not in day.climate, "only what the events call is kept"
    bearish = measured([day], (BEARISH, 15))
    assert [(r.algorithm, r.symbol, r.predicted, r.correct) for r in bearish.rule] == [
        (RULE, B, DOWN, False),
        (RULE, A, DOWN, True),
    ]
    assert bearish.unpublished == 1 and bearish.shares == Counter({DOWN: 1, FLAT: 1})
    j = bearish.against_engine()
    # 000660's bar close has no final consensus: left out, not filled in by another predictor.
    assert (j.scored, j.lift.n, j.accuracy, j.baseline, j.lift.value) == (1, 1, 1.0, 0.0, 1.0)
    c = bearish.against_climate()
    assert (c.lift.n, c.accuracy, c.baseline) == (2, 0.5, 0.5)
    bullish = measured([day], (BULLISH, 30)).against_engine()
    assert (bullish.accuracy, bullish.baseline, bullish.lift.value) == (1.0, 0.0, 1.0)
    assert CALLS == {BEARISH: DOWN, BULLISH: UP}


def hand_day(day: date, outcomes: Sequence[tuple[bool, bool]], claim: tuple[str, int] = (BEARISH, 15)) -> Day:
    """A day of one unit per bar close from 09:31 on (005930 with the futures), each (the rule right,
    the engine right) as ``outcomes`` say; the engine stays out where it is None."""
    direction, minutes = claim
    call = CALLS[direction]
    other = UP if call is DOWN else DOWN
    events, answers, engine = [], {}, {}
    for i, (rule_right, engine_right) in enumerate(outcomes):
        t = at(day, "09:31") + i * BAR
        key = (A, t)
        events.append(SmtEvent(t, A, F, direction, minutes, A, RECONSTRUCTED))
        answer = call if rule_right else (FLAT if engine_right else other)
        answers[key] = answer
        engine[key] = called(
            key, FINAL, answer if engine_right else (FLAT if answer is not FLAT else other), answer
        )
    return measure(day, RECORDED, events, answers, engine, {})


def test_the_verdict_goes_by_trading_day_at_the_corrected_level() -> None:
    # Every day the rule wins by a little more or less: each day's mean gain above 0.
    days = [
        hand_day(d, [(True, False)] * (14 + i % 3) + [(False, True)] * 10 + [(True, True)])
        for i, d in enumerate(HOLDOUT[:DAYS])
    ]
    j = measured(days, (BEARISH, 15)).against_engine()
    means = [(14 + i % 3 - 10) / (25 + i % 3) for i in range(DAYS)]
    expected = t_test(means, LEVEL)
    assert expected is not None and j.by_day == expected and j.days == DAYS
    assert LEVEL == pytest.approx(0.9875) and j.lift.n >= MIN_SCORED
    assert j.verdict == STANDS
    # Strong days and losing ones in turn: the pairs alone would say it wins, the days do not.
    mixed = [
        hand_day(d, [(True, False)] * 30 if i % 2 == 0 else [(True, False)] * 12 + [(False, True)] * 18)
        for i, d in enumerate(HOLDOUT[:DAYS])
    ]
    m = measured(mixed, (BEARISH, 15)).against_engine()
    assert m.lift.low is not None and m.lift.low > 0, "the paired interval counts each pair as independent"
    assert m.by_day is not None and m.by_day.low is not None and m.by_day.low < 0
    assert m.verdict == FAILS


def test_too_few_pairs_or_days_are_not_judged() -> None:
    winning = [(True, False)] * 25
    assert (
        measured([hand_day(d, winning) for d in HOLDOUT[:DAYS]], (BEARISH, 15)).against_engine().verdict
        == STANDS
    )
    # 190 pairs: one short of nothing, but short of 200.
    short = [hand_day(d, winning[:19]) for d in HOLDOUT[:DAYS]]
    assert measured(short, (BEARISH, 15)).against_engine().verdict == TOO_FEW
    # Enough pairs on too few days.
    few_days = [hand_day(d, winning * 2) for d in HOLDOUT[: MIN_DAYS - 1]]
    j = measured(few_days, (BEARISH, 15)).against_engine()
    assert j.lift.n >= MIN_SCORED and j.days == MIN_DAYS - 1 and j.verdict == TOO_FEW
    # The bar closes without a final consensus are not pairs.
    unpublished = [
        replace(d, engine=dict(list(d.engine.items())[:19]))
        for d in (hand_day(d, winning) for d in HOLDOUT[:DAYS])
    ]
    found = measured(unpublished, (BEARISH, 15))
    assert found.unpublished == 60 and found.against_engine().verdict == TOO_FEW
    # A claim without events is too few too, and so are the others here.
    text = "\n".join(holdout_lines(HOLDOUT[:DAYS], short, True))
    assert f"  하락 15: 짝 190 (엔진 예측 없음 0) · 10일 · {TOO_FEW}" in text
    assert f"  상승 30: 짝 0 (엔진 예측 없음 0) · 0일 · {TOO_FEW}" in text
    assert f"{STANDS}: 없음 · {FAILS}: 없음 · {TOO_FEW}: 하락 15, 하락 30, 상승 15, 상승 30" in text


def test_the_holdout_judges_nothing_before_its_window_is_full() -> None:
    text = "\n".join(holdout_lines(HOLDOUT[:3], [], True))
    assert f"창: {AFTER} 뒤 자격이 있는 첫 10거래일. 지금 2026-11-05 – 2026-11-09, 3/10일." in text
    assert text.endswith("창이 아직 차지 않았다. 판정하지 않는다(10일이 차면 판정한다).")
    assert "판정 넷: " not in text
    assert holdout_lines([], [], False)[-1] == "엔진 DB가 없다: 판정하지 않는다."


def test_backfilled_days_are_reported_apart_and_never_against_the_engine() -> None:
    recorded = hand_day(BEFORE, [(True, False)] * 3)
    t = at(BARS_ONLY, "10:01")
    approximate = measure(
        BARS_ONLY,
        BACKFILLED,
        [SmtEvent(t, A, B, BEARISH, 15, B, RECONSTRUCTED)],
        {(A, t): DOWN, (B, t): UP},
        {},
        {},
    )
    text = "\n".join(discovery_lines([recorded], [approximate], True))
    assert (
        "녹화일: 2026-11-03 – 2026-11-03, 1일" in text
        and "채운 1분봉만 있는 날: 2026-10-30 – 2026-10-30, 1일" in text
    )
    events, shares, engine = (text.split(head)[1] for head in ("\n2. ", "\n3. ", "\n4. "))
    assert "  채운 1분봉만 있는 날(1일): 합계 1\n" in events
    assert f"    {A}-{B} bearish 15 {B}: 1 · 1일" in events.split("채운 1분봉만 있는 날")[1]
    assert "  채운 1분봉만 있는 날(근사 정답): 모든 봉 마감 상승 50.0% · 보합 0.0% · 하락 50.0% (2)" in shares
    # Section 4 is the recorded day's alone: its three pairs, the backfilled day's two not among them.
    assert "  하락 15: 짝 3 (엔진 예측 없음 0) · 1일 · 규칙 100.0% · 엔진 0.0% · 차이 +100.0%p" in engine


def test_a_pair_s_minutes_together_and_their_returns_correlation() -> None:
    session = CALENDAR.session(BEFORE)
    assert session is not None
    bars = day_bars({(B, "09:30"): None}, day=BEFORE)  # 000660 has no bar at 09:30
    # Make the returns move together: 005930 and the futures step up at 10:00 and back at 10:01.
    for name, step in ((A, 10.0), (F, 1.0)):
        bars[name] = [
            replace(b, close=b.close + step) if b.start == at(BEFORE, "10:00") else b for b in bars[name]
        ]
    together = co_move(session, bars[A], bars[F])
    assert (together.minutes, together.a, together.b, together.both) == (380, 380, 380, 380)
    assert together.n == 379 and together.correlation == pytest.approx(1.0)
    gap = co_move(session, bars[A], bars[B])
    assert (gap.b, gap.both, gap.n) == (379, 379, 377), "no return into or out of the missing minute"
    assert co_move(session, bars[A], None).both == 0 and co_move(session, bars[A], None).correlation is None


# Reading the data directory and the engine DB


def minute_trades(day: date, prices: Mapping[tuple[str, str], float], last: str = "10:30") -> list[Event]:
    """A trade of each instrument every minute from 09:00 to ``last``, ten seconds in, at the levels
    nothing else in the output could read as (61,230 for the stocks, 345.65 the futures) but for
    ``prices`` by (instrument, minute). In receipt order."""
    events: list[Event] = []
    start = at(day, "09:00")
    while start <= at(day, last):
        traded = start + timedelta(seconds=10)
        for name in (A, B, F):
            price = prices.get((name, f"{start:%H:%M}"), 345.65 if name == F else 61_230.0)
            events.append(
                futures_trade(traded, price) if name == F else trade(traded, int(price), symbol=name)
            )
        start += BAR
    return sorted(events, key=lambda e: e[1]["received_at"])


def store_db(db: Path, rows: Sequence[tuple[str, datetime, Direction | None, Direction | None, str]]) -> None:
    """Each (stock, bar close, answer, the final consensus's call, its status) in the engine DB, with
    the climate's flat call beside it; an answer None is unscorable."""
    store = PredictionStore(db)
    made = []
    for symbol, t, _, call, status in rows:
        for algorithm, forecast, why in (
            (FINAL, SURE[call] if call else None, status),
            (CLIMATE, SURE[FLAT], "ok"),
        ):
            made.append(
                NewPrediction(
                    symbol, algorithm, "v1", t, t, forecast, t + timedelta(minutes=4), 61_230, 61_230, 2, why
                )
            )
    pending = store.add_all(made).pending
    store.score_all(
        [(q.prediction_id, Score(61_230, FLAT, FLAT, None)) for q in pending], max(p.target_at for p in made)
    )
    store.add_touches([touch(symbol, t, answer) for symbol, t, answer, _, _ in rows])
    store.close()


def test_the_holdout_is_the_first_ten_eligible_days_after_the_cutoff_whatever_their_events(
    tmp_path: Path,
) -> None:
    data, db = tmp_path / "data", tmp_path / "state" / DB_NAME
    db.parent.mkdir(parents=True)
    no_consensus, bars_only = HOLDOUT[1], HOLDOUT[2]
    rows = []
    for day in [AFTER, *HOLDOUT]:
        if day != bars_only:
            record(data, minute_trades(day, {}, last="09:05"))  # recorded, no SMT event at all
        status = "no_members" if day == no_consensus else "ok"
        rows.append((A, at(day, "09:01"), UP, UP if status == "ok" else None, status))
    store_backfill(data, bars_only, day_bars(day=bars_only))
    store_db(db, rows)
    now = at(HOLDOUT[-1], "23:00")
    conn = sqlite3.connect(db)
    try:
        found = holdout_days(data, conn, CALENDAR, now)
        assert found == [d for d in HOLDOUT if d not in (no_consensus, bars_only)][:DAYS]
        assert AFTER not in found, "the cutoff's own day is the discovery's"
        assert holdout_days(data, conn, CALENDAR, at(HOLDOUT[3], "20:00")) == [HOLDOUT[0]], (
            "a day once archived"
        )
    finally:
        conn.close()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    data, state = tmp_path / "data", tmp_path / "state"
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))
    monkeypatch.setenv("KRX_SYMBOLS", f"{A},{B}")
    state.mkdir()
    return data, state


def test_the_discovery_reads_the_recording_and_the_db_apart_from_the_backfill_and_prints_no_price(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data, state = env
    # 005930 goes past its high at 10:00 alone: a bearish SMT with 000660 and with the futures at 10:01.
    record(data, minute_trades(BEFORE, {(A, "10:00"): 61_231.0}))
    levels = {A: (61_240.0, 61_220.0), B: (61_239.0, 61_221.0), F: (345.67, 345.61)}
    store_backfill(data, BEFORE, day_bars(day=BEFORE, levels=levels))
    store_backfill(data, BARS_ONLY, day_bars({(B, "11:00"): (61_239, 61_219)}, day=BARS_ONLY, levels=levels))
    t = at(BEFORE, "10:01")
    store_db(
        state / DB_NAME,
        [
            (A, t, DOWN, FLAT, "ok"),  # the rule right, the engine wrong
            (B, t, DOWN, DOWN, "ok"),  # both right
            (A, at(BEFORE, "11:00"), UP, UP, "ok"),  # no event
            (B, at(BEFORE, "11:00"), None, FLAT, "ok"),  # unscorable
        ],
    )
    assert main(["smt-experiment"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"SMT 실험 (#519, #523): 탐색 ({AFTER}까지)\n")
    assert "녹화일: 2026-11-03 – 2026-11-03, 1일\n" in out
    assert "채운 1분봉만 있는 날: 2026-10-30 – 2026-10-30, 1일\n" in out
    recorded = out.split("  녹화일(1일): 합계 ")[1]
    assert recorded.startswith("4\n") and f"    {A}-{B} bearish 15 {A}: 1 · 1일\n" in recorded
    assert f"    {A}-{F} bearish 30 {A}: 1 · 1일\n" in recorded
    assert "  녹화일: 모든 봉 마감 상승 33.3% · 보합 0.0% · 하락 66.7% (3)\n" in out
    assert "    하락 15: 상승 0.0% · 보합 0.0% · 하락 100.0% (2)\n" in out
    assert "  하락 15: 짝 2 (엔진 예측 없음 0) · 1일 · 규칙 100.0% · 엔진 50.0% · 차이 +50.0%p" in out
    assert "  하락 15: 짝 2 · 1일 · 규칙 100.0% · 기후 0.0% · 차이 +100.0%p" in out
    backfilled = out.split("  채운 1분봉만 있는 날(1일): 합계 ")[1]
    assert backfilled.startswith("4\n") and f"    {A}-{B} bullish 15 {B}: 1 · 1일\n" in backfilled
    assert "상관" in out and "함께 있음 100.0%" in out
    for price in ("6121", "6122", "6123", "6124", "345.6"):
        assert price not in out.replace(",", "")


def test_the_holdout_command_judges_the_window_once_it_is_full(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, state = env
    clock = [at(HOLDOUT[0], "23:00")]
    made = smt_experiment.smt_experiment
    monkeypatch.setattr(
        smt_experiment, "smt_experiment", lambda s, c, now, holdout: made(s, c, clock[0], holdout)
    )
    assert main(["smt-experiment", "--holdout"]) == 0
    assert capsys.readouterr().out.strip().endswith("엔진 DB가 없다: 판정하지 않는다.")
    rows = []
    for day in HOLDOUT[:DAYS]:  # each day the same bearish SMT at 10:01, both stocks answering down
        rows += [(A, at(day, "10:01"), DOWN, FLAT, "ok"), (B, at(day, "10:01"), DOWN, DOWN, "ok")]
    store_db(state / DB_NAME, rows)
    sweep = {(A, "10:00"): 61_231.0}
    record(data, minute_trades(HOLDOUT[0], sweep))
    assert main(["smt-experiment", "--holdout"]) == 0
    out = capsys.readouterr().out
    assert "지금 2026-11-05 – 2026-11-05, 1/10일." in out and "판정하지 않는다(10일이 차면" in out
    for day in HOLDOUT[1:DAYS]:
        record(data, minute_trades(day, sweep))
    clock[0] = at(HOLDOUT[DAYS - 1], "23:00")
    assert main(["smt-experiment", "--holdout"]) == 0
    out = capsys.readouterr().out
    assert "지금 2026-11-05 – 2026-11-18, 10/10일." in out
    assert f"  하락 15: 짝 20 (엔진 예측 없음 0) · 10일 · {TOO_FEW}" in out
    assert f"    {A}-{F} bearish 30 {A}: 10 · 10일" in out
    assert "    하락 30: 상승 0.0% · 보합 0.0% · 하락 100.0% (20)" in out
    assert "  하락 15: 짝 20 · 10일 · 규칙 100.0% · 기후 0.0% · 차이 +100.0%p" in out
    for price in ("6122", "6123", "6124", "345.6"):
        assert price not in out.replace(",", "")


# The scheduled jobs


def night(day: date, days: int = 0, record: JobRecord | None = None, commit: str = "abc") -> Night[int]:
    return Night(day, CALENDAR.is_trading_day(day), commit, record, {}, days)


def test_the_discovery_runs_once_on_the_first_night_and_the_verdict_once_its_window_is_full() -> None:
    assert (SMT_DISCOVERY.name, SMT_DISCOVERY.command) == ("smt-discovery-519", ("smt-experiment",))
    assert (SMT_HOLDOUT.name, SMT_HOLDOUT.command) == ("smt-holdout-519", ("smt-experiment", "--holdout"))
    for job in (SMT_DISCOVERY, SMT_HOLDOUT):
        assert [t.issue for t in job.targets] == [519] and job.label == ("ready-for-agent",)
        assert "바꾸지 않는다" in job.targets[0].note and "가격이 없다" in job.targets[0].note
        assert JOBS.index(job) > JOBS.index(NEYMAN_VARIANTS), "last: after the experiments on inputs"
    saturday = date(2026, 11, 7)
    assert SMT_DISCOVERY.due(night(HOLDOUT[0])) and SMT_DISCOVERY.due(night(saturday)), "trading or not"
    ran = JobRecord(HOLDOUT[0], OK, at(HOLDOUT[0], "22:30"), "abc", 0, 60.0)
    assert not SMT_DISCOVERY.due(night(HOLDOUT[1], record=ran)), "once"
    assert SMT_DISCOVERY.due(night(HOLDOUT[1], record=replace(ran, outcome=INTERRUPTED)))
    failed = replace(ran, outcome=FAILED, exit_code=1)
    assert not SMT_DISCOVERY.due(night(HOLDOUT[1], record=failed)), "not again on the same code"
    assert SMT_DISCOVERY.due(night(HOLDOUT[1], record=failed, commit="def")), "again on other code"
    assert not SMT_HOLDOUT.due(night(HOLDOUT[8], DAYS - 1)), "the window is not full"
    assert SMT_HOLDOUT.due(night(HOLDOUT[9], DAYS)) and SMT_HOLDOUT.due(night(saturday, DAYS))
    assert not SMT_HOLDOUT.due(night(HOLDOUT[10], DAYS, ran)), "once"
    assert SMT_HOLDOUT.due(night(HOLDOUT[10], DAYS, failed, "def")), "again on other code after a failure"
