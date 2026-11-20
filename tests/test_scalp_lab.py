"""``krx-quant scalp-lab`` (#711): the mock scalp's variants replayed over the recorded days.

Input: hand-built quote streams (a ``Leg`` of arrays, or a data directory as the collector writes it),
the engine's signals as it stores them (a verdict's 4-minute pool, ``Said``; breiman-scalp's scalp
prediction) and the trader's store with a live scalp trade. Output: each call's trade or why it has
none, the variants' trades one at a time, the walk-forward's pick, the reproduction and the report.

The stock throughout unless said: the bar close 10:00, the signal displayed at 10:00:03, the reference
mid-point 70,050 (tick 100). The first quote after the display asks 70,000: on that fill (commission
0.015 % a side, the sell tax 0.20 %) the target that nets +0.2 % is 70,400 (70,301.7 rounded up to the
tick), the level that nets +0.1 % 70,300 (70,231.5), the stop 0.3 % below 69,790. The ETF's book about
9,000 (tick 5). Every price is made up.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import cli
from krx_quant.config import Settings
from krx_quant.feeds import KRX_BOOK
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.pricing import ETF, SELL_TAX, STOCK
from krx_quant.process_log import Ran
from krx_quant.products import instrument
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.scalp_lab import (
    AGENT_LINE,
    BEFORE_ENTRY,
    BREAK_EVEN,
    ETF_CODE,
    GAP,
    MEMBER,
    MEMBER_ALGORITHM,
    NO_EXIT_QUOTE,
    NO_QUOTE,
    PAST_LAST_EXIT,
    POOL,
    SPECIAL,
    TARGET_REACHED,
    Call,
    Entry,
    Geometry,
    Lab,
    Leg,
    Outcome,
    Tally,
    Variant,
    best,
    entry,
    grid,
    held_out,
    one_at_a_time,
    plan_refused,
    report,
    report_lines,
    thresholds,
    trade,
)
from krx_quant.scheduled import FAILED, JOBS, OK, JobRecord, Night
from krx_quant.scheduled.research import SCALP_LAB, SCALP_LAB_ISSUE, scalp_lab_calls
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from krx_quant.trader import SYMBOL
from krx_quant.trader.book import RAISED_STOP, STOP_LOSS, TAKE_PROFIT, TIME, ScalpRules, halt_spans
from krx_quant.trader.money import net_of
from krx_quant.trader.replay import micros
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import TraderStore
from tests.trader_doubles import CALENDAR, DAY, FEE, Said, at, quote, record_quotes, whole_rules

TODAY = date(*DAY)  # a Wednesday: 09:00 to 15:30
RULES = whole_rules()
NOW = Geometry(0.2, 0.3, 15, 0.1)  # the policy's: +0.2 % net, −0.3 % gross, 15 minutes, raised at +0.1 %
NOT_RAISED = Geometry(0.2, 0.3, 15)
TARGET, RAISE_AT, STOP = 70_400.0, 70_300.0, 70_000 * 0.997
# Standing before the display (not bought on), then the first quote after it.
OPENED = [("10:00:01", 69_700, 69_800), ("10:00:03.5", 69_900, 70_000)]


def leg(
    rows: Sequence[tuple[str, float, float]], code: str = SYMBOL, gaps: Sequence[tuple[str, str]] = ()
) -> Leg:
    times = np.array([micros(at(t)) for t, _, _ in rows], np.int64)
    gap = tuple((micros(at(s)), micros(at(e))) for s, e in gaps)
    return Leg(
        code,
        instrument(code),
        times,
        np.array([b for _, b, _ in rows], float),
        np.array([a for _, _, a in rows], float),
        gap,
    )


def call(close: str = "10:00:00", up: float = 0.35, down: float = 0.25, source: str = POOL) -> Call:
    return Call(source, at(close), at(close) + timedelta(seconds=3), up, down, 70_050.0)


def traded(
    rows: Sequence[tuple[str, float, float]],
    geometry: Geometry = NOW,
    *,
    code: str = SYMBOL,
    gaps: Sequence[tuple[str, str]] = (),
    windows: Sequence[Window] = (),
) -> Outcome | str:
    day = leg(rows, code, gaps)
    c = call()
    found = entry(day, c, 30)
    if not isinstance(found, Entry):
        return found
    return trade(day, c, found, geometry, FEE, windows)


# --- a trade -------------------------------------------------------------------------------------


def test_bought_at_the_first_ask_after_the_display_it_sells_at_the_target_once_a_bid_reaches_it() -> None:
    done = traded([*OPENED, ("10:02:00", 70_300, 70_500), ("10:03:00", 70_400, 70_500)], NOT_RAISED)

    assert isinstance(done, Outcome)
    assert done.reason == TAKE_PROFIT
    assert done.net == pytest.approx(net_of(70_000, TARGET, 1, FEE)), "bought at 70,000, not the 10:00:01 ask"
    assert done.pct == pytest.approx(done.net / 70_000 * 100) and done.pct >= 0.2
    assert done.free == micros(at("10:03:00")) and done.at == micros(at("10:00:03"))
    assert done.loss == pytest.approx(-net_of(70_000, STOP, 1, FEE) / 70_000 * 100)


def test_a_bid_at_or_below_the_stop_sells_there() -> None:
    done = traded([*OPENED, ("10:04:00", 69_700, 69_800)])

    assert isinstance(done, Outcome) and done.reason == STOP_LOSS
    assert done.net == pytest.approx(net_of(70_000, 69_700, 1, FEE))


def test_the_raised_stop_sells_from_the_quote_after_a_bid_reached_its_level() -> None:
    rows = [*OPENED, ("10:01:00", 70_300, 70_400), ("10:02:00", 70_200, 70_300), ("10:14:58", 70_100, 70_200)]

    raised = traded(rows, NOW)
    assert isinstance(raised, Outcome) and raised.reason == RAISED_STOP
    assert raised.net == pytest.approx(net_of(70_000, 70_200, 1, FEE)) and raised.net > 0
    off = traded(rows, NOT_RAISED)
    assert isinstance(off, Outcome) and off.reason == TIME, "without the raise it waits for the deadline"
    assert off.net == pytest.approx(net_of(70_000, 70_100, 1, FEE)), "the 10:14:58 quote, 2 s before 10:15"


def test_a_fall_through_the_stop_after_the_raise_is_the_raised_stop_s() -> None:
    done = traded([*OPENED, ("10:01:00", 70_300, 70_400), ("10:02:00", 69_600, 69_700)])

    assert isinstance(done, Outcome) and done.reason == RAISED_STOP
    assert done.net == pytest.approx(net_of(70_000, 69_600, 1, FEE))


def test_the_raise_level_on_the_last_quote_raises_nothing_it_can_sell_on() -> None:
    done = traded([*OPENED, ("10:14:58", 70_300, 70_400), ("10:15:01", 70_000, 70_100)])

    assert isinstance(done, Outcome) and done.reason == TIME
    assert done.net == pytest.approx(net_of(70_000, 70_300, 1, FEE))


def test_at_the_deadline_a_stale_quote_is_not_sold_on_but_the_first_after_it() -> None:
    done = traded([*OPENED, ("10:14:50", 70_100, 70_200), ("10:15:02", 69_950, 70_050)])

    assert isinstance(done, Outcome) and done.reason == TIME
    assert done.net == pytest.approx(net_of(70_000, 69_950, 1, FEE))
    assert done.free == micros(at("10:15:02"))
    assert traded([*OPENED, ("10:14:50", 70_100, 70_200)]) == NO_EXIT_QUOTE


@pytest.mark.parametrize(
    ("rows", "gaps", "why"),
    [
        ([("10:00:01", 69_900, 70_000), ("10:00:08.1", 69_900, 70_000)], (), NO_QUOTE),  # 5.1 s after
        ([("10:00:01", 69_900, 70_000)], (), NO_QUOTE),  # nothing after the display
        ([("10:00:04", 70_300, 70_400)], (), TARGET_REACHED),  # at the target on the reference already
        (OPENED, (("10:05:00", "10:06:00"),), GAP),
    ],
)
def test_a_call_buys_nothing_without_a_fresh_ask_below_the_target_or_over_a_gap(
    rows: list[tuple[str, float, float]], gaps: tuple[tuple[str, str], ...], why: str
) -> None:
    assert traded(rows, gaps=gaps) == why


def test_a_special_window_known_by_the_entry_buys_nothing() -> None:
    vi = Window(SpecialKind.VI, SYMBOL, at("10:10:00"), at("10:12:00"))
    assert traded(OPENED, windows=[vi]) != SPECIAL, "learned at its start, after the entry"
    early = Window(SpecialKind.VI, SYMBOL, at("09:59:00"), at("10:01:00"))
    assert traded(OPENED, windows=[early]) == SPECIAL


def test_a_market_halt_after_the_entry_fills_no_exit_on_its_reopening_auction_s_book() -> None:
    # A halt from 10:05 to 10:10: its quotes (a bid at the target) are not traded on; the first after it is.
    halt = Window(SpecialKind.MARKET_HALT, None, at("10:05:00"), at("10:10:00"))
    rows = [*OPENED, ("10:06:00", 70_400, 70_500), ("10:10:01", 69_600, 69_700)]
    day = held_out(leg(rows, gaps=(("10:05:00", "10:07:00"),)), halt_spans([halt]))
    c = call()
    found = entry(day, c, 30)
    assert isinstance(found, Entry)

    done = trade(day, c, found, NOT_RAISED, FEE, [halt])
    assert isinstance(done, Outcome) and done.reason == STOP_LOSS, (
        "the gap that began in the halt is its silence"
    )
    assert done.free == micros(at("10:10:01"))
    whole = leg(rows)
    opened = entry(whole, c, 30)
    assert isinstance(opened, Entry)
    unheld = trade(whole, c, opened, NOT_RAISED, FEE, [halt])
    assert isinstance(unheld, Outcome) and unheld.reason == TAKE_PROFIT, "without the halt rule it would fill"


def test_the_session_s_rules_are_the_plan_s() -> None:
    session = CALENDAR.session(TODAY)
    assert session is not None
    assert plan_refused(call("09:04:00"), 15, session, ()) == BEFORE_ENTRY
    assert plan_refused(call("14:50:00"), 30, session, ()) == PAST_LAST_EXIT
    assert plan_refused(call("14:50:00"), 15, session, ()) is None


# --- the ETF -------------------------------------------------------------------------------------


def test_an_etf_trade_uses_its_own_ticks_and_pays_no_sell_tax() -> None:
    assert instrument(ETF_CODE) == ETF and instrument(SYMBOL) == STOCK
    assert (ETF.sell_tax, STOCK.sell_tax) == (0.0, SELL_TAX)
    assert STOCK.netting(70_000, 0.2, FEE) == ScalpRules().target(70_000, FEE) == TARGET
    # 9,005 × 1.00215 ÷ 0.99985 = 9,025.7: the first ETF tick (5 won) at or above it.
    assert ETF.netting(9_005, 0.2, FEE) == 9_030

    done = traded(
        [("09:59:58", 9_000, 9_005), ("10:00:04", 9_000, 9_005), ("10:03:00", 9_030, 9_035)],
        NOT_RAISED,
        code=ETF_CODE,
    )
    assert isinstance(done, Outcome) and done.reason == TAKE_PROFIT
    assert done.net == pytest.approx(9_030 - 9_005 - FEE * (9_030 + 9_005)), "the commissions alone"


# --- one position at a time ----------------------------------------------------------------------


def made(close: str, free: str, pct: float = 0.1) -> Outcome:
    return Outcome(
        micros(at(close)), micros(at(close)) + 3_000_000, micros(at(free)), pct * 700, pct, TIME, 0.2, 0.5
    )


def test_a_call_read_while_a_trade_is_held_is_not_taken_and_one_that_buys_nothing_holds_nothing() -> None:
    first, during, after = (
        made("10:00:00", "10:10:00"),
        made("10:05:00", "10:20:00"),
        made("10:12:00", "10:30:00"),
    )
    trades = [first, during, None, after]

    assert one_at_a_time(trades, [0.4, 0.4, 0.4, 0.4], 0.3) == [first, after]
    assert one_at_a_time(trades, [0.2, 0.4, 0.4, 0.4], 0.3) == [during], "below the threshold, not held"
    assert one_at_a_time(trades, [0.4, 0.4, 0.4, 0.4], [0.5, 0.3, 0.3, 0.3]) == [during]


# --- the ranking and the walk-forward ------------------------------------------------------------


def tally(by_day: dict[date, float], n: int = 12) -> Tally:
    t = Tally()
    for day, pct in by_day.items():
        t.add(day, [made("10:00:00", "10:05:00", pct + 0.01 * k) for k in range(n)])
    return t


def test_the_walk_forward_picks_each_day_s_variant_from_the_days_before_it_alone() -> None:
    d1, d2, d3 = TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)
    steady = Variant(SYMBOL, POOL, 0.3, NOW)
    late = Variant(ETF_CODE, POOL, 0.3, NOW)
    tallies = {steady: tally({d1: 0.05, d2: 0.05, d3: -0.2}), late: tally({d1: 0.04, d2: 0.04, d3: 0.06})}

    assert best(tallies, [d1, d2]) == steady, "steady netted more on the days before d3"
    assert best(tallies) == late
    assert best({steady: tally({d1: 0.05, d2: 0.05}, n=9)}) is None, "fewer than 10 trades a day"

    lines = report_lines(Lab([d1, d2, d3], tallies=tallies), RULES)
    assert f"  {d3}: {steady.label} · 매매 12 · 순손익 {12 * -0.2 + 0.66:+.3f}%" in lines
    assert f"  {d2}: 고른 변형 없음" in lines, "two days before it at least"


def test_the_grid_holds_the_policy_s_geometry_and_raises_only_below_the_target() -> None:
    geometries = grid(NOW)
    assert NOW in geometries and len(geometries) == 45
    assert all(g.raised is None or g.raised < g.target for g in geometries)
    assert Geometry(0.25, 0.3, 15, 0.1) in grid(Geometry(0.25, 0.3, 15, 0.1))


# --- the report, on a recorded day ---------------------------------------------------------------


def record_etf(data: Path, rows: Sequence[tuple[str, int, int]]) -> None:
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for hms, bid, ask in rows:
        received = at(hms)
        event = {
            "symbol": ETF_CODE,
            "event_time": received.replace(microsecond=0),
            "received_at": received.astimezone(UTC),
            "hour_cls": "0",
            "ask_px_1": ask,
            "ask_qty_1": 10,
            "bid_px_1": bid,
            "bid_qty_1": 10,
        }
        recorder.record("events", received.date(), KRX_BOOK.tr_id, ETF_CODE, event, KRX_BOOK.schema)
    recorder.close()


def engine(state: Path) -> None:
    """The 10:00 verdict (its 4-minute pool: up first 0.35, down first 0.25) and breiman-scalp's 10:00
    scalp prediction (target first 0.7), each displayed 3 s after the bar close."""
    store = PredictionStore(state / ENGINE_DB)
    try:
        Said("10:00:00", p=(0.35, 0.4, 0.25), minutes=4).write(store)
        close = at("10:00:00")
        output = {"p_target_first": 0.7, "p_timeout": 0.2, "p_stop_first": 0.1}
        store.add_kind_predictions(
            "scalp",
            [
                NewKindPrediction(
                    symbol=SYMBOL,
                    algorithm=MEMBER_ALGORITHM,
                    model_version="v1",
                    bar_close=close,
                    displayed_at=close + timedelta(seconds=3),
                    output=output,
                    anchor=70_050.0,
                    tick=100,
                )
            ],
        )
    finally:
        store.close()


def live_trade(state: Path, reason: str) -> None:
    """The mock book's live scalp trade of the 10:00 pool, exited for ``reason``."""
    TraderStore(state / TRADER_DB).close()
    db = sqlite3.connect(state / TRADER_DB)
    with db:
        db.execute(
            "INSERT INTO trades (mode, symbol, kind, predictor, prediction_id, bar_close, probability,"
            " threshold, anchor, tick, width_ticks, target, stop, time_barrier, decided_at, entry_at,"
            " entry_price, quantity, buy_fee, exit_at, exit_reason, scalp, scalp_signal)"
            " VALUES ('mock', ?, 'barrier', 'scalp', 1, ?, 0.35, 0.3, 70050, 100, 5, ?, ?, ?, ?, ?, 70000,"
            " 1, 10.5, ?, ?, 1, ?)",
            (
                SYMBOL,
                at("10:00:00").isoformat(),
                TARGET,
                STOP,
                at("10:15:00").isoformat(),
                at("10:00:03").isoformat(),
                at("10:00:03.5").isoformat(),
                at("10:03:00").isoformat(),
                reason,
                json.dumps({"source": "verdict_pool_4m"}),
            ),
        )
    db.close()


def settings(tmp_path: Path) -> Settings:
    return Settings("key", "secret", data_dir=tmp_path / "data", state_dir=tmp_path, fee_rate=FEE)


def test_the_report_ranks_the_variants_and_reproduces_the_live_trade(tmp_path: Path) -> None:
    engine(tmp_path)
    record_quotes(
        tmp_path / "data",
        [quote(t, b, a) for t, b, a in [*OPENED, ("10:03:00", 70_400, 70_500), ("10:40:00", 70_400, 70_500)]],
    )
    record_etf(
        tmp_path / "data",
        [("09:59:58", 9_000, 9_005), ("10:00:04", 9_000, 9_005), ("10:03:00", 8_990, 8_995)],
    )
    live_trade(tmp_path, TAKE_PROFIT)

    with RawDays(tmp_path / "data") as raw:
        out, code = report(settings(tmp_path), CALENDAR, TODAY, raw, RULES)

    assert code == 0
    assert f"재생한 날: {TODAY} – {TODAY}, 1거래일 · 변형 {45 * (4 + 5 + 4 + 4)}개" in out
    assert f"  {TODAY} (풀): 실제 1 · 재생 1 · 같은 봉 마감 1 · 그중 같은 청산 1" in out
    current = Variant(SYMBOL, POOL, 0.3, NOW).label
    assert any(line.startswith(f"  {current} | 1.0 | 100% |") for line in out.splitlines()), out
    assert Variant(SYMBOL, MEMBER, BREAK_EVEN, NOW).label in out
    assert "7. 실행" in out and AGENT_LINE not in out, "one day: no lower bound, nothing qualifies"
    assert "70,000" not in out and "70000" not in out, "no price"


def test_a_configured_pool_threshold_off_the_grid_is_measured_too(tmp_path: Path) -> None:
    assert thresholds(SYMBOL, POOL, 0.35) == (0.2, 0.3, 0.35, 0.4, 0.5)
    assert thresholds(SYMBOL, POOL, 0.3) == thresholds(ETF_CODE, POOL, 0.35) == (0.2, 0.3, 0.4, 0.5)
    engine(tmp_path)
    record_quotes(tmp_path / "data", [quote(t, b, a) for t, b, a in [*OPENED, ("10:03:00", 70_400, 70_500)]])
    rules = whole_rules(scalp=ScalpRules(threshold=0.35))

    with RawDays(tmp_path / "data") as raw:
        out, _ = report(settings(tmp_path), CALENDAR, TODAY, raw, rules)

    current = Variant(SYMBOL, POOL, 0.35, NOW).label
    assert any(line.startswith(f"  {current} | 1.0 | 100% |") for line in out.splitlines()), out


def test_without_a_recorded_day_or_an_engine_database_it_says_so(tmp_path: Path) -> None:
    with RawDays(tmp_path / "data") as raw:
        _, code = report(settings(tmp_path), CALENDAR, TODAY, raw, RULES)
    assert code == 3
    record_quotes(tmp_path / "data", [quote(t, b, a) for t, b, a in OPENED])
    with RawDays(tmp_path / "data") as raw:
        out, code = report(settings(tmp_path), CALENDAR, TODAY, raw, RULES)
    assert code == 1 and "엔진 DB가 없다." in out


# --- the job -------------------------------------------------------------------------------------


def night(day: date, record: JobRecord | None = None) -> Night[None]:
    return Night(day, CALENDAR.is_trading_day(day), "new", record, {}, None)


def ran(day: date, outcome: str = OK) -> JobRecord:
    return JobRecord(day, outcome, datetime(day.year, day.month, day.day, 22, 30, tzinfo=UTC), "new", 0)


def test_the_job_runs_every_trading_night_once_and_posts_to_the_issue() -> None:
    due = SCALP_LAB.due
    assert due(night(TODAY)) and not due(night(TODAY, ran(TODAY)))
    assert due(night(TODAY + timedelta(days=1), ran(TODAY)))
    assert not due(night(TODAY, ran(TODAY, FAILED)))
    assert due(night(date(2026, 10, 17))), "the first night, a Saturday too"
    assert not due(night(date(2026, 10, 17), ran(TODAY))), "then a Saturday is not"
    assert SCALP_LAB in JOBS
    assert callable(cli._parser().parse_args(list(SCALP_LAB.command)).run)
    args = cli._parser().parse_args(["scalp-lab", "--day", "2026-10-14", "--days", "5"])
    assert (args.day, args.days) == (TODAY, 5)
    assert [t.issue for t in SCALP_LAB.targets] == [SCALP_LAB_ISSUE] == [711]


def test_the_job_labels_the_issue_only_when_it_fails_or_calls_an_agent() -> None:
    assert not scalp_lab_calls(Ran(0, "스캘프 실험실 (#711)", ""))
    assert scalp_lab_calls(Ran(0, f"...\n{AGENT_LINE} 처음으로 순위에 든 변형이 있다.", ""))
    assert scalp_lab_calls(Ran(3, "", ""))
    assert scalp_lab_calls(Ran(0, "", "", timed_out=True))
