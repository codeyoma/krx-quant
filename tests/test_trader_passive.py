"""The passive entry's measurement (#432): a buy resting at the best bid against the trader's
marketable entry, on hand-built quotes and KRX trades, worked by hand; the missing data counted
apart; the recordings read a day at a time; the scheduled job.

Input: an engine database written through the prediction store (barrier predictions of breiman and
their answers, as the engine keeps them), the recordings as the collector writes them (the
integrated book, KRX's trades, the gaps), a cost map as the Engine API gives it, with the cell
failing at a market order's round trip. Output: each signal's trades, the tallies and the report.
Every price is made up.

The 4-minute barrier throughout: reference 70,050 won, tick 100, w 5 (target 70,550, stop 69,550),
displayed 2 s after its bar close; commissions 0.015% a side, the sell tax 0.2%. The first quote
after each decision is 70,000 / 70,100 (unless said): the market entry buys at 70,100, the passive
one rests at 70,000.

- A (10:00, up first): KRX trades at 70,000 at 10:00:10 and 69,900 at 10:00:20 (one at 69,900 at
  10:00:02.5, after the decision but before the first quote, does not count); the bid reaches
  70,600 at 10:01:00, the take-profit at 70,550. Market: +450 − (10.515 + 10.5825 + 141.1) =
  +287.8025 won, +2.878025 ticks. Passive (both, optimistic 7 s after the post, conservative 17 s):
  +550 − (10.5 + 10.5825 + 141.1) = +387.8175 won, +3.878175 ticks.
- B (10:10, down first): a KRX trade at 69,900 at 10:10:30 fills both; the bid 69,500 at 10:10:40
  stops all. Market: −600 − 159.94 = −7.5994 ticks; passive: −500 − 159.925 = −6.59925 ticks.
- C (10:20, up first): KRX trades at 70,100, then at 70,000 at 10:20:30 and none below: the
  optimistic fills (27 s), the conservative does not. The bid 70,600 at 10:21:00: take-profit.
- D (10:30, timeout): the first quote locked at 70,100 / 70,100: the passive buy fills at once at
  70,100, as the market's; the time exit at the bid 70,100 of the quote 2 s before the barrier:
  −161.23 won, −1.6123 ticks.
- E (10:40): a gap of KRX's trades of 005930 in its window (another symbol's gap in A's is not).
- F (10:50): no quote within 5 s of its decision.
- G (11:00): its rise's probability 0.55, below θ 0.6.
- H (11:10): the first quote's ask 70,600 at the target already.
- J (the next day, 10:00, up first): the price runs away with no KRX trade at or below the bid:
  the market +2.878025 ticks, neither passive buy fills.
"""

from __future__ import annotations

import re
import weakref
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from krx_quant.barriers import DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.config import Settings
from krx_quant.entry_delay import Quotes
from krx_quant.feeds import BOOK, KRX_TRADE
from krx_quant.feeds.base import KST
from krx_quant.health import GAP_SCHEMA
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.recorder import ParquetRecorder
from krx_quant.scheduled import (
    FAILED,
    JOBS,
    OK,
    PASSIVE_ENTRY,
    TRADER_REPORT,
    Facts,
    JobRecord,
    Night,
)
from krx_quant.trader import SYMBOL, passive
from krx_quant.trader.cells import Cell
from krx_quant.trader.paper import STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.passive import (
    CONSERVATIVE,
    GAP,
    NO_EXIT_QUOTE,
    NO_QUOTE,
    NO_RECORDING,
    NOT_MEASURED,
    OPTIMISTIC,
    Eligible,
    Recording,
    Tally,
    measure,
    measure_one,
    micros,
    report,
    run_passive_entry,
    sale_of,
)
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.test_trader_refit import barrier, engine
from tests.test_trader_run import EngineApi
from tests.trader_doubles import CALENDAR, DAY, FEE, cost_map, cost_row, predictor

BREIMAN = Cell("barrier", "breiman")
FIRST, SECOND = date(*DAY), date(2026, 10, 15)  # a Wednesday and a Thursday: both trade
NEXT = (2026, 10, 15)

# The hand-worked trades (module doc), in ticks a share.
MARKET_UP = (70_550 - 70_100 - (70_100 * FEE + 70_550 * FEE + 70_550 * 0.002)) / 100
PASSIVE_UP = (70_550 - 70_000 - (70_000 * FEE + 70_550 * FEE + 70_550 * 0.002)) / 100
MARKET_DOWN = (69_500 - 70_100 - (70_100 * FEE + 69_500 * FEE + 69_500 * 0.002)) / 100
PASSIVE_DOWN = (69_500 - 70_000 - (70_000 * FEE + 69_500 * FEE + 69_500 * 0.002)) / 100
FLAT = -(70_100 * FEE * 2 + 70_100 * 0.002) / 100


def test_the_hand_worked_figures() -> None:
    assert [MARKET_UP, PASSIVE_UP] == pytest.approx([2.878025, 3.878175])
    assert [MARKET_DOWN, PASSIVE_DOWN] == pytest.approx([-7.5994, -6.59925])
    assert FLAT == pytest.approx(-1.6123)


def moment(day: date, hms: str) -> datetime:
    whole, _, fraction = hms.partition(".")
    h, m, s = (int(x) for x in whole.split(":"))
    micro = int((fraction + "000000")[:6]) if fraction else 0
    return datetime(day.year, day.month, day.day, h, m, s, micro, tzinfo=KST)


# The recordings, as the collector writes them.

QUOTES = {
    FIRST: [
        ("09:59:00", 70_000, 70_100),
        ("10:00:01", 70_000, 70_100),  # before A's decision
        ("10:00:03", 70_000, 70_100),  # A's first quote
        ("10:00:15", 69_900, 70_000),
        ("10:00:25", 70_000, 70_100),
        ("10:01:00", 70_600, 70_700),  # A's take-profit
        ("10:05:00", 70_000, 70_100),
        ("10:10:03", 70_000, 70_100),  # B's first quote
        ("10:10:20", 69_900, 70_000),
        ("10:10:40", 69_500, 69_600),  # B's stop
        ("10:15:00", 70_000, 70_100),
        ("10:20:03", 70_000, 70_100),  # C's first quote
        ("10:21:00", 70_600, 70_700),  # C's take-profit
        ("10:25:00", 70_100, 70_100),
        ("10:30:03", 70_100, 70_100),  # D's first quote: locked
        ("10:33:58", 70_100, 70_200),  # D's time exit: the last quote before its barrier
        ("10:34:01", 70_000, 70_100),
        ("10:40:03", 70_000, 70_100),  # E's first quote: E is in a gap
        ("10:45:00", 70_000, 70_100),
        ("10:50:09", 70_000, 70_100),  # 7 s after F's decision: too late
        ("11:00:03", 70_000, 70_100),
        ("11:10:03", 70_500, 70_600),  # H's first quote: its ask at the target already
        ("11:20:00", 70_500, 70_600),
    ],
    SECOND: [
        ("09:59:00", 70_000, 70_100),
        ("10:00:03", 70_000, 70_100),  # J's first quote
        ("10:00:30", 70_600, 70_700),  # J's take-profit
        ("10:05:00", 70_600, 70_700),
    ],
}
KRX_TRADES = {
    FIRST: [
        ("10:00:02.500", 69_900),  # after A's decision, before its first quote: no fill
        ("10:00:10", 70_000),  # A: at the bid (optimistic)
        ("10:00:20", 69_900),  # A: below it (conservative)
        ("10:10:30", 69_900),  # B: both
        ("10:20:05", 70_100),  # C: above the bid
        ("10:20:30", 70_000),  # C: at the bid (optimistic alone)
        ("10:30:10", 70_100),
        ("11:00:05", 70_000),
    ],
    SECOND: [("10:00:05", 70_100), ("10:00:31", 70_600)],
}
GAPS = [
    (KRX_TRADE.tr_id, SYMBOL, "10:41:00", "10:41:30"),  # in E's window
    (KRX_TRADE.tr_id, "000660", "10:01:00", "10:01:30"),  # another symbol's, in A's window
]


def record(data: Path, days: tuple[date, ...] = (FIRST, SECOND)) -> None:
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for day in days:
        for hms, bid, ask in QUOTES[day]:
            at = moment(day, hms)
            row = {"symbol": SYMBOL, "event_time": at, "received_at": at.astimezone(UTC), "hour_cls": "0"}
            row |= {"bid_px_1": bid, "ask_px_1": ask, "bid_qty_1": 10, "ask_qty_1": 10}
            recorder.record("events", day, BOOK.tr_id, SYMBOL, row, BOOK.schema)
        for hms, price in KRX_TRADES[day]:
            at = moment(day, hms)
            row = {"symbol": SYMBOL, "event_time": at, "received_at": at.astimezone(UTC), "venue": "KRX"}
            row |= {"price": price, "qty": 1, "side": "sell"}
            recorder.record("events", day, KRX_TRADE.tr_id, SYMBOL, row, KRX_TRADE.schema)
    for feed, key, start, end in GAPS:
        row = {"feed": feed, "symbol": key, "start": moment(FIRST, start), "end": moment(FIRST, end)}
        recorder.record("gaps", FIRST, feed, key, row | {"reason": "disconnected: test"}, GAP_SCHEMA)
    recorder.close()


# The predictions, as the engine keeps them.

PREDICTIONS = [
    (FIRST, "10:00", (0.75, 0.15, 0.10), UP_FIRST),  # A
    (FIRST, "10:10", (0.65, 0.20, 0.15), DOWN_FIRST),  # B
    (FIRST, "10:20", (0.70, 0.20, 0.10), UP_FIRST),  # C
    (FIRST, "10:30", (0.80, 0.15, 0.05), TIMEOUT),  # D
    (FIRST, "10:40", (0.90, 0.05, 0.05), UP_FIRST),  # E
    (FIRST, "10:50", (0.90, 0.05, 0.05), UP_FIRST),  # F
    (FIRST, "11:00", (0.55, 0.30, 0.15), UP_FIRST),  # G
    (FIRST, "11:10", (0.90, 0.05, 0.05), UP_FIRST),  # H
    (SECOND, "10:00", (0.75, 0.15, 0.10), UP_FIRST),  # J
]


def write_predictions(state: Path) -> None:
    store = engine(state)
    try:
        for day, close, p, label in PREDICTIONS:
            barrier(store, close, p, label, day=(day.year, day.month, day.day))
    finally:
        store.close()


FAILING = cost_map([cost_row("barrier", top=predictor("breiman", 0.55, "fail"))])
BODY = {**FAILING, "rows": [{**r, "days": [FIRST.isoformat(), SECOND.isoformat()]} for r in FAILING["rows"]]}


def measured(tmp_path: Path) -> passive.Measurement:
    write_predictions(tmp_path)
    record(tmp_path / "data")
    return measure(
        BODY,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        tmp_path / "data",
        CALENDAR.session,
        fee_rate=FEE,
        target_version=3,
        threshold=0.6,
    )


def test_fills_and_p_and_l_on_a_hand_built_path(tmp_path: Path) -> None:
    """The module doc's signals: the market entry trades A, B, C, D and J; the conservative passive
    buy fills A, B and D (D at once, locked), the optimistic one C too; neither fills J."""
    done = measured(tmp_path)

    [cell] = done.cells
    assert (cell.cell, cell.role, cell.threshold) == (BREIMAN, "top", 0.6), "a failing cell is measured too"
    assert cell.counts == {"read": 9, "below_threshold": 1, "signals": 8}
    t = done.tallies[BREIMAN]
    assert (t.signals, dict(t.missing), t.target_reached, t.measured) == (8, {GAP: 1, NO_QUOTE: 1}, 1, 5)
    assert (done.days, done.recorded) == ((FIRST, SECOND), (FIRST, SECOND))

    market = t.market
    assert (market.trades, market.hits) == (5, 3)
    assert market.net == pytest.approx(3 * MARKET_UP + MARKET_DOWN + FLAT)
    assert market.exits == {TAKE_PROFIT: 3, STOP_LOSS: 1, TIME: 1}

    conservative = t.passive[CONSERVATIVE]
    assert (conservative.filled.trades, conservative.filled.hits, conservative.marketable) == (3, 1, 1)
    assert (conservative.unfilled, conservative.unfilled_hits) == (2, 2), "C and J ran away: hits both"
    assert conservative.filled.net == pytest.approx(PASSIVE_UP + PASSIVE_DOWN + FLAT)
    assert conservative.market_net == pytest.approx(MARKET_UP + MARKET_DOWN + FLAT)
    assert sorted(conservative.waits) == [0.0, 17.0, 27.0]
    assert conservative.filled.exits == {TAKE_PROFIT: 1, STOP_LOSS: 1, TIME: 1}
    assert {d: pytest.approx(v) for d, v in conservative.differences.items()} == {
        FIRST: [PASSIVE_UP - MARKET_UP, PASSIVE_DOWN - MARKET_DOWN, -MARKET_UP, 0.0],
        SECOND: [-MARKET_UP],
    }

    optimistic = t.passive[OPTIMISTIC]
    assert (optimistic.filled.trades, optimistic.filled.hits, optimistic.marketable) == (4, 2, 1)
    assert (optimistic.unfilled, optimistic.unfilled_hits) == (1, 1)
    assert optimistic.filled.net == pytest.approx(2 * PASSIVE_UP + PASSIVE_DOWN + FLAT)
    assert optimistic.market_net == pytest.approx(2 * MARKET_UP + MARKET_DOWN + FLAT)
    assert sorted(optimistic.waits) == [0.0, 7.0, 27.0, 27.0]

    assert done.total.measured == done.kinds["barrier"].measured == 5, "one cell: the same counts"


def test_the_report_shows_ticks_ratios_and_counts_and_no_price(tmp_path: Path) -> None:
    text = report(measured(tmp_path))

    assert text.startswith(
        "대기 매수 측정 (#432): 005930, 비용 지도 20d(as_of 2026-10-14T08:30:00+09:00) · 칸 1개"
    )
    assert "θ: KRX_TRADER_THRESHOLD 0.6(챔피언 정책 없음)" in text
    assert (
        "전체: 신호 8 · 데이터 빠짐 2(결측 구간 1, 결정 뒤 호가 없음 1) · 목표가 이미 닿음 1 · 잰 신호 5"
        in text
    )
    market_net = (3 * MARKET_UP + MARKET_DOWN + FLAT) / 5
    assert f"  시장가: 맞힘률 60.0% · 순손익 {market_net:+.2f}틱/매매 · 익절 3, 손절 1, 시간 1" in text
    assert (
        "  대기 보수적(그 가격 아래 체결): 체결 3/5 = 60.0%(맞물려 바로 1) · 맞힘률: 체결 33.3%, "
        "미체결 100.0%(시장가 60.0%) · 체결까지 중앙 17.0초" in text
    )
    per_signal = (PASSIVE_UP + PASSIVE_DOWN + FLAT) / 5
    per_fill = (PASSIVE_UP + PASSIVE_DOWN + FLAT) / 3
    assert f"    순손익: 신호당 {per_signal:+.2f}틱(미체결 0), 체결당 {per_fill:+.2f}틱 · " in text
    assert "대기 − 시장가 신호당" in text and "날 단위 95%" in text and ", 2일)" in text
    assert "칸 barrier breiman (top, θ 0.6): 읽은 예측 9 · 뺀 것: θ 미만 1" in text
    assert re.search(r"메모리: 최대 RSS (\d+\.\d|\?) MB", text), text
    assert not re.search(r"(69|70)[,.]?\d{3}", text), "no price in the output"


# Each rule by itself, on quotes made up.


def quotes(day: date, *rows: tuple[str, float, float]) -> Quotes:
    return Quotes(
        np.array([micros(moment(day, t)) for t, _, _ in rows], dtype=np.int64),
        np.array([bid for _, bid, _ in rows], dtype=np.float64),
        np.array([ask for _, _, ask in rows], dtype=np.float64),
    )


def signal(close: str = "10:00", day: date = FIRST, minutes: int = 4) -> Eligible:
    at = moment(day, close + ":00")
    return Eligible(
        BREIMAN, at + timedelta(seconds=2), 70_550, 69_550, at + timedelta(minutes=minutes), 100, True
    )


def test_the_time_exit_sells_on_the_last_quote_before_the_barrier_if_recent_else_on_the_next() -> None:
    barrier = micros(moment(FIRST, "10:04:00"))
    entered = micros(moment(FIRST, "10:00:03"))
    recent = quotes(
        FIRST, ("10:00:03", 70_000, 70_100), ("10:03:56", 70_200, 70_300), ("10:04:01", 70_300, 70_400)
    )
    assert sale_of(recent, entered, 70_550, 69_550, barrier) == (70_200, TIME), "4 s before it"
    stale = quotes(
        FIRST, ("10:00:03", 70_000, 70_100), ("10:03:50", 70_200, 70_300), ("10:04:01", 70_300, 70_400)
    )
    assert sale_of(stale, entered, 70_550, 69_550, barrier) == (70_300, TIME), "10 s before: the next one's"
    ended = quotes(FIRST, ("10:00:03", 70_000, 70_100), ("10:03:50", 70_200, 70_300))
    assert sale_of(ended, entered, 70_550, 69_550, barrier) is None, "nothing to sell on"


def test_a_quote_at_or_after_the_barrier_sells_on_time_whatever_its_bid() -> None:
    barrier = micros(moment(FIRST, "10:04:00"))
    after = quotes(
        FIRST, ("10:00:03", 70_000, 70_100), ("10:03:58", 70_100, 70_200), ("10:04:00", 70_700, 70_800)
    )
    assert sale_of(after, micros(moment(FIRST, "10:00:03")), 70_550, 69_550, barrier) == (70_100, TIME)


def test_without_a_quote_to_sell_on_the_signal_is_missing_for_both() -> None:
    day = Recording(
        quotes(FIRST, ("10:00:03", 70_000, 70_100), ("10:01:00", 70_100, 70_200)),
        np.array([micros(moment(FIRST, "10:00:10"))], dtype=np.int64),
        np.array([69_900.0]),
    )
    assert measure_one(signal(), day, FEE).missing == NO_EXIT_QUOTE


def test_an_open_gap_leaves_out_every_signal_after_its_start() -> None:
    day = Recording(
        quotes(FIRST, ("10:00:03", 70_000, 70_100)),
        np.array([], dtype=np.int64),
        np.array([], dtype=np.float64),
        ((micros(moment(FIRST, "09:30:00")), None),),
    )
    assert measure_one(signal(), day, FEE).missing == GAP


def test_a_day_without_its_recording_is_missing(tmp_path: Path) -> None:
    write_predictions(tmp_path)
    record(tmp_path / "data", days=(FIRST,))

    done = measure(
        BODY,
        tmp_path / ENGINE_DB,
        tmp_path / TRADER_DB,
        tmp_path / "data",
        CALENDAR.session,
        fee_rate=FEE,
        target_version=3,
        threshold=0.6,
    )

    assert done.recorded == (FIRST,)
    assert done.tallies[BREIMAN].missing == {GAP: 1, NO_QUOTE: 1, NO_RECORDING: 1}, "J's day: not recorded"


def test_the_champion_s_threshold_is_the_cell_s(tmp_path: Path) -> None:
    from krx_quant.trader import policy as policies

    db = policies.connect(tmp_path / TRADER_DB)
    try:
        cell = policies.PolicyCell("barrier", "top", "breiman", 0.7, 0.5)
        policies.add(db, moment(FIRST, "22:31:00"), "test", (cell,), (), {})
    finally:
        db.close()

    done = measured(tmp_path)

    [read] = done.cells
    assert read.threshold == 0.7
    assert read.counts == {"read": 9, "below_threshold": 2, "signals": 7}, "B (0.65) and G (0.55) under 0.7"
    assert "θ: 챔피언 정책 1의 칸 값" in report(done)


def test_the_recordings_are_read_a_day_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each day's recording is let go before the next is read: two are never held at once."""
    alive: list[weakref.ref[Recording]] = []
    read_day = passive.read_day

    def tracked(data_dir: Path, day: date) -> Recording | None:
        assert all(ref() is None for ref in alive), "the day before is let go first"
        found = read_day(data_dir, day)
        if found is not None:
            alive.append(weakref.ref(found))
        return found

    monkeypatch.setattr(passive, "read_day", tracked)

    done = measured(tmp_path)

    assert len(alive) == 2 and done.recorded == (FIRST, SECOND)


def test_a_tally_of_nothing_reports_its_counts_alone() -> None:
    from krx_quant.trader.passive import _group

    assert _group("전체", Tally()) == ["전체: 신호 0 · 잰 신호 0"]


# The command and its job.


async def test_the_command_reads_tonight_s_cost_map_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_predictions(tmp_path)
    record(tmp_path / "data")
    settings = Settings(
        kis_app_key="", kis_app_secret="", state_dir=tmp_path, data_dir=tmp_path / "data", fee_rate=FEE
    )
    api = EngineApi([BODY])

    async with api.serving() as url:
        code = await run_passive_entry(settings, TraderSettings(engine_url=url))

    assert code == 0
    assert api.asked == [{"symbol": SYMBOL, "period": "20d", "special": "exclude"}]
    out = capsys.readouterr().out
    assert out.startswith("대기 매수 측정 (#432): 005930")
    assert "잰 신호 5" in out
    assert not (tmp_path / TRADER_DB).exists(), "read-only: no store is made"


async def test_without_the_cost_map_nothing_is_measured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    api = EngineApi([503])

    async with api.serving() as url:
        code = await run_passive_entry(settings, TraderSettings(engine_url=url))

    assert code == NOT_MEASURED
    assert "비용 지도를 읽지 못해 재지 않았다" in capsys.readouterr().out


MONDAY, TUESDAY, SATURDAY = date(2026, 11, 2), date(2026, 11, 3), date(2026, 10, 31)


def night(day: date, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), "abc", record, {}, Facts())


def ran(day: date, outcome: str, code: int) -> JobRecord:
    return JobRecord(day, outcome, moment(day, "22:40:00"), "abc", code, 30.0)


def test_the_job_runs_on_a_trading_night_until_a_run_measures() -> None:
    assert PASSIVE_ENTRY.command == ("measure-passive-entry",)
    assert [t.issue for t in PASSIVE_ENTRY.targets] == [432]
    assert PASSIVE_ENTRY.timeout == timedelta(minutes=15)
    assert PASSIVE_ENTRY.due(night(MONDAY))
    assert not PASSIVE_ENTRY.due(night(SATURDAY)), "not a trading day"
    assert not PASSIVE_ENTRY.due(night(TUESDAY, ran(MONDAY, OK, 0))), "once measured"
    assert PASSIVE_ENTRY.due(night(TUESDAY, ran(MONDAY, FAILED, NOT_MEASURED))), "again after a failed run"
    assert not PASSIVE_ENTRY.due(night(MONDAY, ran(MONDAY, FAILED, NOT_MEASURED))), "once a night"
    assert PASSIVE_ENTRY.due(night(TUESDAY, ran(MONDAY, "interrupted", 0))), "a run a restart cut"


def test_the_job_runs_after_the_trader_s_night_and_before_the_simulations() -> None:
    first: Any = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(TRADER_REPORT) < JOBS.index(PASSIVE_ENTRY) < first
