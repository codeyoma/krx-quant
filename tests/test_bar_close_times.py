"""The night's report of how long a trading day's bar closes took (#589, ``bar_close_times.py``)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.bar_close_times import bar_close_times, tonight
from krx_quant.baselines import Forecast
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds import KST
from krx_quant.loop_watch import LOOP_HELD, Collected, held_detail
from krx_quant.predictions import DB_NAME, NewKindPrediction, NewPrediction, PredictionStore
from krx_quant.scheduled import BAR_CLOSE_TIMES, FAILED, INTERRUPTED, OK, JobRecord, Night
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
    timed out; two on the day, ito 0.01 s and 0.02 s after them, both holding the loop (1.1 s, and
    1.4 s with a garbage collection of 0.62 s in it, #621); a quote stall of 6.0 s on each day.
    Output: each day's bar closes, first record, final consensus and stalls; each algorithm's seconds
    after the first record, the day's against the days before; the day's bar closes that held the
    loop, how many a garbage collection ran in, and their largest step. Exit 0."""
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(BEFORE, 0), ito=0.5, last=0.9)
    bar_close(store, close(BEFORE, 1), ito=0.7, last=1.1)
    stored(store, "ulam", close(BEFORE, 1), 10.2, status="timed_out")
    bar_close(store, close(DAY, 0), ito=0.01, last=0.3)
    bar_close(store, close(DAY, 1), ito=0.02, last=0.4)
    store.add_run(LOOP_HELD, close(DAY, 0), None, held_detail(1.1, [("records", 0.9), ("other", 0.2)]))
    held = [("records", 1.1), ("other", 0.3)]
    store.add_run(LOOP_HELD, close(DAY, 1), None, held_detail(1.4, held, Collected(0.62, 2)))
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
    assert lines[-2].endswith("`loop_held`): 루프 막힘 2번(가장 긴 1.40초, GC가 겹친 것 1번)")
    assert lines[-1] == "가장 큰 단계: records 2번"


def test_an_open_stall_is_told_apart_with_its_time_up_to_now(tmp_path: Path) -> None:
    """Input: on the day, a stall that ended after 6 s and one from 10:30 that has not ended, read at
    13:30. Output: one stall of 6.0 s, and one not ended, three hours up to now: not its 5 s of
    detection."""
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(DAY, 0), ito=0.01, last=0.3)
    store.close()
    trader = TraderStore(tmp_path / TRADER_DB_NAME)
    quote = close(DAY, 0) - timedelta(seconds=1)
    ended = trader.add_stall("mock", quote + timedelta(seconds=5), quote)
    trader.end_stall(ended.id, quote + timedelta(seconds=6), "quote")
    last = close(DAY, 30)
    trader.add_stall("mock", last + timedelta(seconds=5), last)

    report, code = bar_close_times(settings(tmp_path), datetime.combine(DAY, time(13, 30), KST))

    assert code == 0
    assert "호가 끊김 1번(가장 긴 6.0초), 끝나지 않은 끊김 1번(지금까지 가장 긴 10800초)" in report


def test_a_day_without_predictions_fails(tmp_path: Path) -> None:
    """Input: no engine DB, then one with a day before only, asked for the day. Output: exit 1 both
    times, saying why."""
    assert bar_close_times(settings(tmp_path), datetime.combine(DAY, time(22, 40), KST))[1] == 1
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(BEFORE, 0), ito=0.5, last=0.9)
    store.close()

    report, code = bar_close_times(settings(tmp_path), datetime.combine(DAY, time(22, 40), KST), day=DAY)

    assert code == 1
    assert report == f"봉 마감 시간 (#589): {DAY}의 예측이 없다"


def test_the_job_reads_its_own_night_s_day_never_an_earlier_one(tmp_path: Path) -> None:
    """Input: predictions on a day before only; the job's run on the day's night, at 22:40 and after
    midnight. Output: tonight is the day both times, and the run fails: the predictions before the
    deploy are not its evidence. Once the day has predictions, it reports the day."""
    late, after_midnight = datetime.combine(DAY, time(22, 40), KST), datetime.combine(DAY, time(23), KST)
    after_midnight += timedelta(hours=3)
    assert tonight(late) == tonight(after_midnight) == DAY
    assert tonight(datetime.combine(DAY, time(6), KST)) == DAY
    store = PredictionStore(tmp_path / DB_NAME)
    bar_close(store, close(BEFORE, 0), ito=0.5, last=0.9)

    assert bar_close_times(settings(tmp_path), late, tonight(late)) == (
        f"봉 마감 시간 (#589): {DAY}의 예측이 없다",
        1,
    )
    bar_close(store, close(DAY, 0), ito=0.01, last=0.3)
    store.close()
    report, code = bar_close_times(settings(tmp_path), after_midnight, tonight(after_midnight))
    assert code == 0 and report.startswith(f"봉 마감 시간 (#589) · {DAY}, 앞 거래일 1일")
    assert BAR_CLOSE_TIMES.command == ("bar-close-times", "--tonight")


def night(day: date, trading: bool, record: JobRecord | None, commit: str = "c1") -> Night[None]:
    return Night(day, trading, commit, record, {}, None)


def test_the_job_runs_on_trading_nights_until_a_run_ends_ok() -> None:
    """The first trading night; after a failure or a cut run, the next trading night on the same code
    too, not the night it failed on; never after a run that ended ok."""
    started = datetime.combine(DAY, time(22, 40), KST)
    ran = JobRecord(DAY, OK, started, "c1", exit_code=0)
    failed = JobRecord(DAY, FAILED, started, "c1", exit_code=1)
    cut = JobRecord(DAY, INTERRUPTED, started, "c1")
    tuesday = DAY + timedelta(days=1)

    assert not BAR_CLOSE_TIMES.due(night(date(2026, 11, 7), False, None))
    assert BAR_CLOSE_TIMES.due(night(DAY, True, None))
    assert not BAR_CLOSE_TIMES.due(night(DAY, True, failed))
    assert BAR_CLOSE_TIMES.due(night(tuesday, True, failed))
    assert BAR_CLOSE_TIMES.due(night(tuesday, True, cut))
    assert not BAR_CLOSE_TIMES.due(night(date(2026, 11, 14), False, failed))
    assert not BAR_CLOSE_TIMES.due(night(tuesday, True, ran))
    assert [t.issue for t in BAR_CLOSE_TIMES.targets] == [589]
