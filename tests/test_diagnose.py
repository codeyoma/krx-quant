"""The read-only diagnose command (#82): the measurements the open decisions need, as text.

Input: a data directory with a fixed engine DB, recorded events and macro readings, made
by hand. Output: what ``krx-quant diagnose`` prints, against figures worked out by hand;
and the directory and DB as they were.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.macro import INDICATORS, READING_SCHEMA, REGIME_SCHEMA
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score, iso
from krx_quant.pricing import Direction
from krx_quant.recorder import ParquetRecorder
from krx_quant.touch import SILENT, Reached, TouchResult
from tests.replay_events import DAY, SYMBOL, at, book, futures_trade, trade
from tests.test_replay_cli import record

EARLIER = date(2026, 10, 8)
FORECAST = Forecast(0.5, 0.3, 0.2, 1.0)
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN


def predicted(
    store: PredictionStore, bar_close: datetime, delay: float, *, status: str = "ok", special: bool = False
) -> int:
    """newton's prediction at ``bar_close``, stored ``delay`` seconds later."""
    added = store.add_all(
        [
            NewPrediction(
                symbol=SYMBOL,
                algorithm="newton",
                model_version="v1",
                bar_close=bar_close,
                displayed_at=bar_close + timedelta(seconds=delay),
                forecast=FORECAST if status == "ok" else None,
                target_at=bar_close + timedelta(minutes=4),
                mid_close=81_250.0,
                mid_display=81_250.0,
                flat_ticks=2,
                status=status,
                special=special,
            )
        ]
    )
    return added.ids["newton"][0]


def scored(store: PredictionStore, prediction_id: int, a: Direction, b: Direction) -> None:
    store.score_all([(prediction_id, Score(81_350.0, a, b, None))], datetime(2026, 10, 12, 16, 0, tzinfo=UTC))


def macro_day(
    recorder: ParquetRecorder, day: date, reasons: list[str], missing: set[str], empty: set[str]
) -> None:
    """The day's regime, and a reading of every indicator but ``missing`` (``empty`` without a value)."""
    stamp = datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=1)
    for indicator in INDICATORS:
        if indicator.name in missing:
            continue
        row = {
            "day": day,
            "recorded_at": stamp,
            "indicator": indicator.name,
            "source": indicator.source.value,
            "series": indicator.series,
            "as_of": None if indicator.name in empty else day,
            "value": None if indicator.name in empty else 1.0,
            "change_pct": None,
        }
        recorder.record("macro", day, "readings", indicator.name, row, READING_SCHEMA)
    regime = {
        "day": day,
        "recorded_at": stamp,
        "regime": "neutral",
        "reasons": json.dumps(reasons, ensure_ascii=False),
        "failed": "{}",
        "skipped": "[]",
    }
    recorder.record("macro", day, "regime", "market", regime, REGIME_SCHEMA)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A server's data and state directories, as the diagnose command reads them."""
    data, state = tmp_path / "data", tmp_path / "state"
    store = PredictionStore(state / DB_NAME)
    # 2026-10-08: one prediction, displayed 10 s after its bar close, scored A up, B up.
    scored(store, predicted(store, at("09:01:00", EARLIER), 10.0), UP, UP)
    # 2026-10-12: four displayed 0.5, 1, 2 and 4 s late; three scored (B differs once,
    # one special), one still pending; and a prediction that timed out.
    scored(store, predicted(store, at("09:01:00"), 0.5), UP, UP)
    scored(store, predicted(store, at("09:02:00"), 1.0), UP, FLAT)
    scored(store, predicted(store, at("09:03:00"), 2.0, special=True), DOWN, DOWN)
    predicted(store, at("09:04:00"), 4.0)
    predicted(store, at("09:05:00"), 11.0, status="timed_out")
    # Their touch labels (#167), at 81,250 (k = 2, targets 81,450 and 81,050): 09:01 up at
    # 09:02:10, after it was displayed; 09:02 up at 09:02:00.4, before its display at 09:02:01;
    # 09:03 flat, though a trade printed at 81,450; 09:04 silent.
    store.add_touches(
        [
            touched(at("09:01:00"), up_at=at("09:02:10"), dwell=0.5, max_bid=81_500, trade_high=81_500),
            touched(at("09:02:00"), up_at=at("09:02:00.4"), dwell=2.0, max_bid=81_450, trade_high=81_450),
            touched(at("09:03:00"), up_at=None, dwell=None, max_bid=81_400, trade_high=81_450),
            TouchResult(SYMBOL, at("09:04:00"), None, SILENT),
        ],
        at("09:10:00"),
    )
    store.close()
    # An earlier target version's label of the same 09:01 window: not this version's, left out.
    conn = sqlite3.connect(state / DB_NAME)
    with conn:
        conn.execute(
            "INSERT INTO touches (symbol, bar_close, target_version, class, unscorable, path, scored_at)"
            " SELECT symbol, bar_close, 1, 'down', unscorable, path, scored_at FROM touches"
            " WHERE bar_close = ?",
            (iso(at("09:01:00")),),
        )
    conn.close()
    record(
        data,
        [
            # The book: 2 ticks wide before the 09:01 close, 1 tick before 09:02's, then silent.
            book(at("09:00:20"), 81_200, 81_300),
            book(at("09:00:50.7"), 81_200, 81_400),
            trade(at("09:00:31.5"), 81_250, traded=at("09:00:30")),
            book(at("09:01:30.2"), 81_200, 81_300),
            futures_trade(at("09:01:40.25"), 352.5),
        ],
    )
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    macro_day(recorder, EARLIER, ["반도체 강세"], missing={"usdkrw"}, empty={"vix"})
    macro_day(recorder, DAY, ["지표 없음"], missing=set(), empty=set())
    recorder.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))
    monkeypatch.setenv("KRX_SYMBOLS", SYMBOL)
    return tmp_path


def touched(
    close: datetime, *, up_at: datetime | None, dwell: float | None, max_bid: float, trade_high: float
) -> TouchResult:
    """A window at 81,250 (targets 81,450 and 81,050) whose ask never fell below 81,300."""
    reached = (
        Reached(up_at, max_bid, None, up_at + timedelta(seconds=dwell))
        if up_at is not None and dwell is not None
        else None
    )
    return TouchResult(
        SYMBOL,
        close,
        UP if reached else FLAT,
        None,
        anchor=81_250,
        tick=100,
        k=2,
        upper=81_450,
        lower=81_050,
        upper_reached=reached,
        max_bid=max_bid,
        min_ask=81_300,
        trade_high=trade_high,
        trade_low=81_200,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file's bytes under ``root``: the data, the DB and any file made beside it."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_measurements_are_reported_as_worked_out_by_hand(
    server: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(server)

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    # Quantiles are the lower of the two values around q·(n − 1).
    assert "진단 2026-10-08 – 2026-10-12 (2거래일)" in out
    # Delays 0.5, 1, 2, 4, 10 s; four scored on both bases, B differs from A once.
    assert (
        "  newton: 예측 5, 지연 p50 2.00 · p90 4.00 · p99 4.00 · 최대 10.00"
        " · 채점 4, A≠B 25.0% (둘 다 채점 4)" in out
    )
    assert "  채점 4, 특수 상황 1 (25.0%)" in out
    assert "  상태: ok 5, timed_out 1" in out
    # 09:01: 2 ticks (the 09:00:50 book); 09:02: 1 tick; 09:03 on: the book is over a minute old.
    assert "  005930: 봉 마감 2, p50 1 · p90 1 · p99 1 · 최대 2" in out
    # Books received 0, 0.7 and 0.2 s after their exchange second; the trade 1.5 s after.
    assert "  H0UNASP0: 3건, p50 0.20 · p90 0.20 · p99 0.20 · 최대 0.70" in out
    assert "  H0UNCNT0: 1건, p50 1.50 · p90 1.50 · p99 1.50 · 최대 1.50" in out
    assert (
        "  H0IFCNT0: 1건, p50 0.25 · p90 0.25 · p99 0.25 · 최대 0.25"
        " · 거래소 시각이 수신 초인 행: REST 대체 행, 장 마감 기록" in out
    )
    assert "  지표 없음: 2026-10-12" in out
    assert "  일부 빠짐: 2026-10-08 (usdkrw, vix)" in out
    assert "  기록 없음: 없음" in out
    # The touch labels: two up and one flat scored, one silent.
    assert "  005930: 상승 67% · 보합 33% · 하락 0% (3창)" in out
    assert "  채점 불가: silent quotes 1" in out
    # At k + 1 (81,550) and 2k (81,650) none of the highest bids (81,500 at most) reach.
    assert "k+1 상승 0% · 보합 100% · 하락 0% (3창)" in out
    assert "  체결가만 목표에 닿고 호가는 닿지 않음 33% · 위아래 둘 다 닿음 0%" in out
    assert "  맞은 상승·하락 예측 2개 중 표시 전에 이미 닿음 1개 (50.0%)" in out
    assert "  목표에 머문 시간(초): p50 0.5 · p90 0.5 · p99 0.5 · 최대 2.0 · 1초 미만 50%" in out
    assert snapshot(server) == before, "nothing is written, not even beside the DB"


def test_days_narrows_the_range_to_the_latest_trading_days(
    server: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["diagnose", "--days", "1"]) == 0

    out = capsys.readouterr().out
    assert "진단 2026-10-12 – 2026-10-12 (1거래일)" in out
    assert (
        "  newton: 예측 4, 지연 p50 1.00 · p90 2.00 · p99 2.00 · 최대 4.00 · 채점 3, A≠B 33.3% (둘 다 채점 3)"
        in out
    )
    assert "  채점 3, 특수 상황 1 (33.3%)" in out
    assert "  상태: ok 4, timed_out 1" in out
    assert "  일부 빠짐: 없음" in out


def test_only_the_sections_without_data_say_so(server: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(server / "data")

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    assert "  newton: 예측 5," in out
    sections = out.split("\n\n")
    assert [s.splitlines()[1] for s in sections[3:6]] == ["  없음", "  없음", "  없음"]


def test_a_section_without_its_data_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    assert out.count("  없음") == 6
    assert not (tmp_path / "state").exists() and not (tmp_path / "data").exists()
