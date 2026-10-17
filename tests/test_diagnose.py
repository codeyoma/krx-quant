"""The read-only diagnose command (#82): the measurements the open decisions need, as text.

Input: a data directory with a fixed engine DB, recorded events and macro readings, made
by hand; and a recorded day of crossed quotes alone (#187). Output: what ``krx-quant
diagnose`` prints, against figures worked out by hand; and the directory and DB as they were.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.barriers import AMBIGUOUS, BARRIER
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.engine import Event
from krx_quant.feeds import KRX_TRADE, NXT_TRADE
from krx_quant.kinds import Answer
from krx_quant.macro import INDICATORS, READING_SCHEMA, REGIME_SCHEMA
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score, iso
from krx_quant.pricing import Direction
from krx_quant.recorder import ParquetRecorder
from krx_quant.touch import SILENT, TARGET_VERSION, Reached, TouchResult
from tests.replay_events import DAY, SYMBOL, at, book, futures_trade, trade, vi
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
    # 2026-10-08: one prediction, displayed 10 s after its bar close, its four minutes over
    # (bases A and B up), but its bar close has no touch label: not scored (#170).
    scored(store, predicted(store, at("09:01:00", EARLIER), 10.0), UP, UP)
    # 2026-10-12: four displayed 0.5, 1, 2 and 4 s late; three scored on their touch labels
    # below (one special; the old bases, unread, differ once), one still pending; and a
    # prediction that timed out.
    scored(store, predicted(store, at("09:01:00"), 0.5), UP, UP)
    scored(store, predicted(store, at("09:02:00"), 1.0), UP, FLAT)
    scored(store, predicted(store, at("09:03:00"), 2.0, special=True), DOWN, DOWN)
    predicted(store, at("09:04:00"), 4.0)
    predicted(store, at("09:05:00"), 11.0, status="timed_out")
    store.record_models([("newton", "v1", TARGET_VERSION)])  # trained on the touch target (#170)
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
    # The range's answers (#180), at 81,200–81,300 (k = 2): 09:01 medium (2.5 ticks: 1.5 up, 1
    # down), 09:02 small (1: half up, half down), 09:31 large (4 up); 09:03 special (left out),
    # 09:04 silent. And 09:01's answer of another kind, not the range's.
    store.add_kind_answers(
        "range",
        1,
        [
            (SYMBOL, at("09:01:00"), ranged("medium", 2.5, 1.5, 1.0), False),
            (SYMBOL, at("09:02:00"), ranged("small", 1.0, 0.5, 0.5), False),
            (SYMBOL, at("09:31:00"), ranged("large", 4.0, 4.0, 0.0), False),
            (SYMBOL, at("09:03:00"), ranged("medium", 3.0, 3.0, 0.0), True),
            (SYMBOL, at("09:04:00"), Answer(None, SILENT, {}), False),
        ],
    )
    store.add_kind_answers("drift", 1, [(SYMBOL, at("09:01:00"), Answer("rose", None, {"r": 9.0}), False)])
    # The barrier's answers (#183), at 81,250 (k = 2): 09:01 timeout (w 2), 09:02 up first (w 3),
    # 09:31 up first and 09:41 down first (w 2); 09:03 special (left out), 09:04 ambiguous.
    store.add_kind_answers(
        "barrier",
        BARRIER.answer_version,
        [
            (SYMBOL, at("09:01:00"), barriered("timeout", 2), False),
            (SYMBOL, at("09:02:00"), barriered("up_first", 3), False),
            (SYMBOL, at("09:31:00"), barriered("up_first", 2), False),
            (SYMBOL, at("09:41:00"), barriered("down_first", 2), False),
            (SYMBOL, at("09:03:00"), barriered("down_first", 2), True),
            (SYMBOL, at("09:04:00"), Answer(None, AMBIGUOUS, {}), False),
        ],
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
    backfilled(data)
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    macro_day(recorder, EARLIER, ["반도체 강세"], missing={"usdkrw"}, empty={"vix"})
    macro_day(recorder, DAY, ["지표 없음"], missing=set(), empty=set())
    recorder.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))
    monkeypatch.setenv("KRX_SYMBOLS", SYMBOL)
    return tmp_path


def barriered(label: str, w: int) -> Answer:
    """A barrier answer at 81,250 (k = 2, the cost 1.86875 ticks) of width ``w``: bought at the
    ask 81,300 or sold at the bid 81,200 at the bar close."""
    path = {
        "w": w,
        "k": 2,
        "tick": 100,
        "anchor": 81_250.0,
        "upper": 81_250.0 + w * 100,
        "lower": 81_250.0 - w * 100,
        "cost": 1.86875,
        "bid_start": 81_200.0,
        "ask_start": 81_300.0,
    }
    return Answer(label, None, path)


def ranged(label: str, r: float, up: float, down: float) -> Answer:
    """A range answer at 81,250 (k = 2) of ``r`` ticks, ``up`` and ``down`` of it."""
    return Answer(label, None, {"r": r, "k": 2, "tick": 100, "anchor": 81_250.0, "up": up, "down": down})


# The day's backfilled bars (start, high, low, close): the bar rule's input. From 09:01 (close
# 81,200): 81,400 to 81,200 by 09:04, 2 ticks (c = 0 medium as the quotes; c = 1 small); from
# 09:02 (close 81,300): 81,300 to 81,200, 1 tick (small either way, as the quotes); from 09:31
# (close 81,300): 81,700 to 81,300, 4 ticks (c = 0 large as the quotes; c = 1 medium).
# The barrier's bar rule (#183), w = k = 2 (fewer than twenty bars, no Friday): from 09:01 (the
# close 81,200 at its low: U 81,600, L 80,900) nothing reached, timeout as the quotes; from 09:02
# (81,300, the next open the same: U 81,600, L 81,000) timeout, the quotes up first; from 09:31
# (81,300 at its high: U 81,600, L 80,900) 09:31's 81,700, up first as the quotes; from 09:41
# (81,300, one price: U 81,600, L 81,000) both in 09:41's bar: the same minute.
BARS = [
    ("09:00:00", 81_300, 81_200, 81_200),
    ("09:01:00", 81_400, 81_200, 81_300),
    ("09:02:00", 81_300, 81_200, 81_300),
    ("09:03:00", 81_300, 81_300, 81_300),
    ("09:04:00", 81_300, 81_200, 81_200),
    ("09:05:00", 81_200, 81_200, 81_200),
    ("09:30:00", 81_300, 81_200, 81_300),
    ("09:31:00", 81_700, 81_300, 81_600),
    ("09:32:00", 81_600, 81_500, 81_500),
    ("09:40:00", 81_300, 81_300, 81_300),
    ("09:41:00", 81_800, 80_800, 81_300),
]


def backfilled(data: Path) -> None:
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": at(start),
            "venue": "UN",
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10,
            "cum_value": 0,
        }
        for start, high, low, close in BARS
    ]
    path = data / "bars" / f"date={DAY}" / f"feed={MINUTE_BARS_TR}" / f"symbol={SYMBOL}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


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
    # Delays 0.5, 1, 2, 4, 10 s; three scored on their touch labels (#170: no A and B).
    assert "  newton: 예측 5, 지연 p50 2.00 · p90 4.00 · p99 4.00 · 최대 10.00 · 채점 3\n" in out
    assert "A≠B" not in out
    assert "  채점 3, 특수 상황 1 (33.3%)" in out
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
    # The range's answers (#180), the special one left out: by symbol and slot, r's spread.
    assert "  005930 09:00: 작음 50% · 보통 50% · 큼 0% (2창) · r p50 1 · p90 1 · p99 1 · 최대 2.5" in out
    assert "  005930 09:30: 작음 0% · 보통 0% · 큼 100% (1창) · r p50 4 · p90 4 · p99 4 · 최대 4" in out
    assert "  특수 상황 1창(뺌) · silent quotes 1 (채점 불가)" in out
    # The bar rule: c = 0 gives all three classes, c = 1 only 09:02's.
    assert (
        "  1분봉 규칙과 호가 정답의 구간 일치율: c = 0 100.0% · c = 1 33.3% (3창)"
        " · 더 많이 같은 c 0 (지금 BAR_C 1)" in out
    )
    assert "    005930: c = 0 100.0% · c = 1 33.3% (3창)" in out
    assert "  1분봉 범위 − 호가 범위(틱, c = 0): p50 0 · p90 0 · p99 0 · 최대 0" in out
    assert (
        "  005930 위로 간 폭(틱): p50 1.5 · p90 1.5 · p99 1.5 · 최대 4"
        " | 아래로 간 폭(틱): p50 0.5 · p90 0.5 · p99 0.5 · 최대 1" in out
    )
    # The barrier's answers (#183), the special one left out.
    assert "  005930 09:00: 위 먼저 50% · 시간 만료 50% · 아래 먼저 0% (2창)" in out
    assert "  005930 09:30: 위 먼저 50% · 시간 만료 0% · 아래 먼저 50% (2창)" in out
    assert "  특수 상황 1창(뺌) · ambiguous 1 (채점 불가)" in out
    assert "  005930 폭 w(틱): p50 2 · p90 2 · p99 2 · 최대 3 · w = k 75% (4창)" in out
    # The bar rule: 09:01 and 09:31 as the quotes, 09:02 not; 09:41 in the same minute.
    assert "  1분봉 규칙과 호가 정답의 클래스 일치율: 66.7% (3창) · 같은 분 양쪽 닿음 25.0% (4창 중)" in out
    assert "    005930: 66.7% (3창) · 같은 분 양쪽 닿음 25.0% (4창 중)" in out
    # Every call right: 09:02 long to U (81,550): 2.5 − 1.86875; 09:31 long (81,450) and 09:41
    # short (81,050): 1.5 − 1.86875 each; 09:01 timeout, no trade. Their mean −0.035, its
    # standard error 1/3: ± 1.96 / 3.
    assert (
        "  005930 미래를 알았을 때의 장벽 손익(틱, 연구용): 거래 3 · 합계 -0.1 · 거래당 -0.04"
        " (95% -0.69–0.62)" in out
    )
    # The book (#187): three updates from the open, none crossed; the trade's quotes are empty.
    assert (
        "  005930 2026-10-12: 통합 호가 3건 중 뒤집힘 0.0% · 시간 0.0% · 뒤집힘 0번"
        " · 통합 체결 1호가 1건 중 뒤집힘 0.0%\n    30분 칸(호가/시간): 09:00 0.0%/0.0%\n" in out
    )
    assert (
        "  005930 뒤집힌 통합 호가 없음\n"
        "  005930 봉 마감 직전 호가가 뒤집힘: 376개 중 0개 (날마다 0–0)\n" in out
    )
    # Skill and entry delay (#262): newton's two scored calls, both up and right (09:03's special
    # left out). One class called and happened: no kappa, no Gerrity. At their bar closes both
    # reached the upper target (the stored paths); acted on later they see the recorded quotes
    # only: from 09:01:01 nothing reaches 81,450 (the book at 09:01:30.2 bids 81,200), and after
    # 09:02 nothing was received. The stored paths keep no start quotes: no touch P&L.
    assert (
        "  newton: 채점 2 · 정답률 100.0% · 기대 정답률 100.0% · kappa — (거래일 —) · Gerrity —"
        " · 상승·하락 예측 2\n"
        "    목표 도달: 봉 마감 100.0% · +1초 0.0% · +5초 0.0% · +30초 0.0%\n"
        "    닿음 손익(틱/번, 연구용): 봉 마감 (n 0) — · +1초 (n 0) — · +5초 (n 0) — · +30초 (n 0) —\n" in out
    )
    assert snapshot(server) == before, "nothing is written, not even beside the DB"


def test_days_narrows_the_range_to_the_latest_trading_days(
    server: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["diagnose", "--days", "1"]) == 0

    out = capsys.readouterr().out
    assert "진단 2026-10-12 – 2026-10-12 (1거래일)" in out
    assert "  newton: 예측 4, 지연 p50 1.00 · p90 2.00 · p99 2.00 · 최대 4.00 · 채점 3\n" in out
    assert "  채점 3, 특수 상황 1 (33.3%)" in out
    assert "  상태: ok 4, timed_out 1" in out
    assert "  일부 빠짐: 없음" in out


def test_only_the_sections_without_data_say_so(server: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(server / "data")

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    assert "  newton: 예측 5," in out
    sections = out.split("\n\n")
    assert [s.splitlines()[1] for s in sections[3:6] + sections[9:10]] == ["  없음"] * 4
    # Without the recorded quotes only the bar close is measured (#262).
    assert "    목표 도달: 봉 마감 100.0% · +1초 (n 0) — · +5초 (n 0) — · +30초 (n 0) —" in out
    assert "  1분봉 규칙: 정답이 있는 날의 1분봉 없음" in out, "the range's answers, without the bars"


def test_a_section_without_its_data_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    assert out.count("  없음") == 10
    assert not (tmp_path / "state").exists() and not (tmp_path / "data").exists()


CROSSED = "000660"


def coded(event: Event, hour_cls: str) -> Event:
    """``event`` with its raw time code (``HOUR_CLS_CODE``)."""
    feed, row = event
    return feed, {**row, "hour_cls": hour_cls}


def crossed_book(when: datetime, bid: int, ask: int) -> Event:
    return book(when, bid, ask, CROSSED)


def venue_trade(when: datetime, venue: str, price: int, bid: int, ask: int) -> Event:
    """A trade on one venue, carrying that venue's best quotes."""
    feed = KRX_TRADE if venue == "KRX" else NXT_TRADE
    return trade(when, price, bid=bid, ask=ask, feed=feed, symbol=CROSSED)


# A day of 000660 (ticks of 100 won) with five crossed spells of the integrated book (#187).
CROSSED_DAY = [
    # 1. 09:00:58–09:01:01 (3 s; 1 tick, then 2): KRX's bid 81,400 above NXT's ask 81,200, both
    # traded at. It spans the 09:01 bar close: its last quote, an integrated trade's, is crossed,
    # and the reference price used the trade's valid quote of 09:00:45, 15 s old.
    crossed_book(at("09:00:40"), 81_200, 81_300),
    trade(at("09:00:45"), 81_250, bid=81_200, ask=81_300, symbol=CROSSED),
    crossed_book(at("09:00:58"), 81_300, 81_200),
    crossed_book(at("09:00:59.5"), 81_400, 81_200),
    venue_trade(at("09:00:59.6"), "KRX", 81_400, 81_400, 81_500),
    venue_trade(at("09:00:59.7"), "NXT", 81_200, 81_100, 81_200),
    trade(at("09:00:59.8"), 81_300, bid=81_400, ask=81_200, symbol=CROSSED),
    crossed_book(at("09:01:01"), 81_200, 81_300),
    # 2. 10:00:00–10:01:30 (90 s, over a minute) in a VI (10:00:05–10:02:10), the book's time code
    # C: KRX's own book crossed. At the 10:01 close the last valid quote is 70 s old: no mid.
    crossed_book(at("09:59:50"), 81_200, 81_300),
    coded(crossed_book(at("10:00:00"), 81_400, 81_300), "C"),
    coded(crossed_book(at("10:00:30"), 81_400, 81_300), "C"),
    coded(venue_trade(at("10:00:40"), "KRX", 81_350, 81_400, 81_300), "C"),
    coded(crossed_book(at("10:01:00"), 81_400, 81_300), "C"),
    crossed_book(at("10:01:30"), 81_300, 81_400),
    vi(at("10:02:15"), "10:00:05", "10:02:10", CROSSED),
    trade(at("10:30:00"), 81_300, bid=81_300, ask=81_400, symbol=CROSSED),
    venue_trade(at("10:30:00"), "KRX", 81_300, 81_300, 81_400),
    # 3. 11:00:00–11:00:02: NXT's bid 81,300 above KRX's ask 81,200, traded at NXT's bid only.
    crossed_book(at("11:00:00"), 81_300, 81_200),
    venue_trade(at("11:00:01"), "NXT", 81_300, 81_300, 81_400),
    crossed_book(at("11:00:02"), 81_200, 81_300),
    # 4. 11:30:00–11:30:01: a KRX trade whose quotes are on neither side.
    crossed_book(at("11:30:00"), 81_300, 81_200),
    venue_trade(at("11:30:00.5"), "KRX", 81_250, 81_100, 81_250),
    crossed_book(at("11:30:01"), 81_200, 81_300),
    venue_trade(at("12:00:00"), "NXT", 81_250, 81_200, 81_300),
    # 5. 13:00:00–13:00:00.5: no trade on either venue.
    crossed_book(at("13:00:00"), 81_300, 81_200),
    crossed_book(at("13:00:00.5"), 81_200, 81_300),
]


def test_crossed_quotes_are_measured_as_worked_out_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    record(data, CROSSED_DAY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    before = snapshot(tmp_path)

    assert main(["diagnose"]) == 0

    out = capsys.readouterr().out
    section = out.split("\n\n")[9].splitlines()
    assert section[0].startswith("9. 뒤집힌 호가(#187)")
    # Fifteen updates, eight crossed. Each holds until the next, a minute at most: 424.5 s in
    # all, 96.5 s crossed. By slot: 09:00 3 s of 81 crossed; 10:00 90 of 150; 11:00 2 of 62;
    # 11:30 1 of 61; 13:00 0.5 of 60.5. One integrated trade's quotes of three crossed.
    assert section[1:3] == [
        "  000660 2026-10-12: 통합 호가 15건 중 뒤집힘 53.3% · 시간 22.7% · 뒤집힘 5번"
        " · 통합 체결 1호가 3건 중 뒤집힘 33.3%",
        "    30분 칸(호가/시간): 09:00 50.0%/3.7% · 09:30 0.0%/0.0% · 10:00 75.0%/60.0%"
        " · 11:00 50.0%/3.2% · 11:30 50.0%/1.6% · 13:00 50.0%/0.8%",
    ]
    # Spells of 0.5, 1, 2, 3 and 90 s; seven crossed updates 1 tick deep, one 2.
    assert section[3:6] == [
        "  000660 한 번 뒤집혀 있는 시간(초, 5번): p50 2.0 · p90 3.0 · p99 3.0 · 최대 90.0",
        "  000660 깊이(틱, 뒤집힌 호가 8건): p50 1 · p90 1 · p99 1 · 최대 2",
        "  000660 60초 넘게 뒤집힘: 2026-10-12 10:00:00부터 90.0초",
    ]
    # The 09:01 and 10:01 closes of the 376.
    assert section[6] == (
        "  000660 봉 마감 직전 호가가 뒤집힘: 376개 중 2개 (날마다 2–2)"
        " · 그때 쓴 유효 호가의 나이(초): p50 15.0 · p90 15.0 · p99 15.0 · 최대 15.0"
        " · 유효 호가 없음(중간가 모름) 1개"
    )
    # A spell each; their time 3, 2, 90, 1 and 0.5 s of 96.5.
    assert section[7] == (
        "  000660 거래소 귀속(뒤집힘 5번): KRX 매수 > NXT 매도 20.0% (시간 3.1%)"
        " · NXT 매수 > KRX 매도 20.0% (시간 2.1%) · KRX 자체 뒤집힘 20.0% (시간 93.3%)"
        " · NXT 자체 뒤집힘 0.0% (시간 0.0%) · 섞임 0.0% (시간 0.0%)"
        " · 통합과 맞지 않음 20.0% (시간 1.0%) · 거래소별 체결 없음 20.0% (시간 0.5%)"
    )
    assert section[8] == (
        "  000660 뒤집힌 가격에 체결: KRX 매수 > NXT 매도 1번 중 높은 매수호가(KRX)에 체결 100.0%"
        " · 낮은 매도호가(NXT)에 체결 100.0% · 둘 다 100.0%"
        " | NXT 매수 > KRX 매도 1번 중 높은 매수호가(NXT)에 체결 100.0%"
        " · 낮은 매도호가(KRX)에 체결 0.0% · 둘 다 0.0%"
    )
    assert section[9:12] == [
        # The 90 s spell from 10:00:00 overlaps the VI from 10:00:05: 85 s of 96.5.
        "  000660 VI와 겹친 뒤집힘: 1번 (뒤집힌 시간의 88.1%) · 그 날들의 VI 1번",
        "  000660 통합 호가의 시간 구분 코드(원문): 뒤집힌 호가 0 62.5% · C 37.5% (8건)"
        " | 그 밖 0 100.0% (7건)",
        "  000660 거래소별 체결의 장운영/시간 구분 코드(원문): 뒤집힌 동안"
        " KRX 0/0 66.7% · 0/C 33.3% (3건), NXT 0/0 100.0% (2건)"
        " | 그 밖 KRX 0/0 100.0% (1건), NXT 0/0 100.0% (1건)",
    ]
    assert section[12].startswith("  거래소별 1호가는 거래소별 체결(H0STCNT0 KRX, H0NXCNT0 NXT)에 실린")
    assert len(section) == 13
    assert snapshot(tmp_path) == before, "nothing is written"


def test_a_one_sided_quote_after_a_crossed_one_is_the_close_s_last_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Input: a crossed update at 09:00:58, then a one-sided one (no ask) at 09:00:59, before the
    09:01 close. Output: the close's last quote is the one-sided one, not crossed: no crossed
    close, though the reference price still falls back on the valid quote of 09:00:40."""
    data = tmp_path / "data"
    record(
        data,
        [
            crossed_book(at("09:00:40"), 81_200, 81_300),
            crossed_book(at("09:00:58"), 81_300, 81_200),
            crossed_book(at("09:00:59"), 81_300, 0),
            crossed_book(at("09:01:30"), 81_200, 81_300),
        ],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["diagnose"]) == 0

    section = capsys.readouterr().out.split("\n\n")[9].splitlines()
    assert "  000660 봉 마감 직전 호가가 뒤집힘: 376개 중 0개 (날마다 0–0)" in section
