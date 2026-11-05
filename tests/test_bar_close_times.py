"""The night's report of how long a trading day's bar closes took (#589, ``bar_close_times.py``)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.bar_close_times import bar_close_times
from krx_quant.baselines import Forecast
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds import KST
from krx_quant.loop_watch import LOOP_HELD, held_detail
from krx_quant.predictions import DB_NAME, NewKindPrediction, NewPrediction, PredictionStore
from krx_quant.scheduled import BAR_CLOSE_TIMES, FAILED, OK, Facts, JobRecord, Night
from krx_quant.trader.store import DB_NAME as TRADER_DB_NAME
from krx_quant.trader.store import TraderStore

BEFORE, DAY = date(2026, 11, 6), date(2026, 11, 9)
SYMBOLS = ("005930", "000660")
EVEN = Forecast(0.3, 0.4, 0.3, 0.0)


def settings(state: Path) -> Settings:
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=state / "data",
        state_dir=state,
    )


def close(day: date, minute: int) -> datetime:
    return datetime.combine(day, time(10, minute), KST)


def stored(store: PredictionStore, name: str, bar_close: datetime, after: float, status: str = "ok") -> None:
    """``name``'s prediction of each symbol at ``bar_close``, stored ``after`` seconds past it."""
    store.add_all(
        [
            NewPrediction(
                symbol=symbol,
                algorithm=name,
                model_version="v1",
                bar_close=bar_close,
                displayed_at=bar_close + timedelta(seconds=after),
                forecast=EVEN if status == "ok" else None,
                target_at=bar_close + timedelta(minutes=4),
                mid_close=None,
                mid_display=None,
                flat_ticks=None,
                status=status,
            )
            for symbol in SYMBOLS
        ]
    )


def bar_close(store: PredictionStore, at: datetime, ito: float, last: float) -> None:
    """One bar close as the engine stores it: the baselines 0.2 s after it, newton 0.05 s after them,
    ito ``ito`` s after them, the final consensus at ``last`` s, the range's Mandelbrot 0.1 s after
    the baselines."""
    stored(store, "baseline-climate", at, 0.2)
    stored(store, "newton", at, 0.25)
    stored(store, "ito", at, 0.2 + ito)
    stored(store, FINAL, at, last)
    store.add_kind_predictions(
        "range",
        [
            NewKindPrediction(s, "mandelbrot", "v1", at, at + timedelta(seconds=0.3), {"p_wide": 1.0})
            for s in SYMBOLS
        ],
    )


def test_the_report_compares_the_day_s_bar_closes_with_the_days_before(tmp_path: Path) -> None:
    """Input: two bar closes on a day before, ito 0.5 s and 0.7 s after the baselines, one of them
    timed out; two on the day, ito 0.01 s and 0.02 s after them, one holding the loop 1.4 s; a quote
    stall of 6.0 s on each day. Output: each day's bar closes, first record, final consensus and
    stalls; each algorithm's seconds after the first record, the day's against the days before; the
    day's bar close that held the loop and its largest step. Exit 0."""
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(BEFORE, 0), ito=0.5, last=0.9)
    bar_close(store, close(BEFORE, 1), ito=0.7, last=1.1)
    stored(store, "ulam", close(BEFORE, 1), 10.2, status="timed_out")
    bar_close(store, close(DAY, 0), ito=0.01, last=0.3)
    bar_close(store, close(DAY, 1), ito=0.02, last=0.4)
    store.add_run(LOOP_HELD, close(DAY, 1), None, held_detail(1.4, [("records", 1.1), ("other", 0.3)]))
    store.close()
    trader = TraderStore(tmp_path / TRADER_DB_NAME)
    for day in (BEFORE, DAY):
        quote = close(day, 0) - timedelta(seconds=1)
        stall = trader.add_stall("mock", quote + timedelta(seconds=5), quote)
        trader.end_stall(stall.id, quote + timedelta(seconds=6), "quote")

    report, code = bar_close_times(settings(tmp_path), datetime.combine(DAY, time(22, 40), KST))

    assert code == 0
    lines = report.splitlines()
    assert lines[0] == f"봉 마감 시간 (#589) · {DAY}, 앞 거래일 1일({BEFORE}–{BEFORE})과 견준다"
    assert f"- {BEFORE}: 봉 마감 2 · 첫 저장 0.200/0.200/0.200초 · 최종 합의 0.900/0.900/1.100초" in report
    assert f"- {DAY}: 봉 마감 2 · 첫 저장 0.200/0.200/0.200초 · 최종 합의 0.300/0.300/0.400초" in report
    assert report.count("호가 끊김 1번(가장 긴 6.0초)") == 2
    assert "- ito: 0.010/0.010/0.020초 · 0.500/0.500/0.700초 · 시간 초과·오류 0/0" in lines
    assert "- newton: 0.050/0.050/0.050초 · 0.050/0.050/0.050초 · 시간 초과·오류 0/0" in lines
    assert "- ulam: 없음 · 10.000/10.000/10.000초 · 시간 초과·오류 0/1" in lines
    assert "- mandelbrot: 0.100/0.100/0.100초 · 0.100/0.100/0.100초 · 시간 초과·오류 0/0" in lines
    assert not any(line.startswith(("- baseline-climate", f"- {FINAL}")) for line in lines)
    assert lines[-2].endswith("`loop_held`): 루프 막힘 1번(가장 긴 1.40초)")
    assert lines[-1] == "가장 큰 단계: records 1번"


def test_a_day_without_predictions_fails(tmp_path: Path) -> None:
    """Input: no engine DB, then one with a day before only, asked for the day. Output: exit 1 both
    times, saying why."""
    assert bar_close_times(settings(tmp_path), datetime.combine(DAY, time(22, 40), KST))[1] == 1
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(BEFORE, 0), ito=0.5, last=0.9)
    store.close()

    report, code = bar_close_times(settings(tmp_path), datetime.combine(DAY, time(22, 40), KST), day=DAY)

    assert code == 1
    assert report == f"봉 마감 시간 (#589): {DAY}까지 예측이 없다"


def night(day: date, trading: bool, record: JobRecord | None, commit: str = "c2") -> Night:
    return Night(day, trading, commit, record, {}, Facts())


def test_the_job_runs_once_on_the_first_trading_night_and_again_on_other_code_after_a_failure() -> None:
    started = datetime.combine(DAY, time(22, 40), KST)
    ran = JobRecord(DAY, OK, started, "c1", exit_code=0)
    failed = JobRecord(DAY, FAILED, started, "c1", exit_code=1)

    assert not BAR_CLOSE_TIMES.due(night(date(2026, 11, 7), False, None))
    assert BAR_CLOSE_TIMES.due(night(DAY, True, None))
    assert not BAR_CLOSE_TIMES.due(night(DAY + timedelta(days=1), True, ran))
    assert not BAR_CLOSE_TIMES.due(night(DAY + timedelta(days=1), True, failed, commit="c1"))
    assert BAR_CLOSE_TIMES.due(night(DAY + timedelta(days=1), True, failed))
    assert BAR_CLOSE_TIMES.command == ("bar-close-times",)
    assert [t.issue for t in BAR_CLOSE_TIMES.targets] == [589]
