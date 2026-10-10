"""``krx-quant fit <algorithm>``: train on backfilled bars, save the model the engine loads."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import ALGORITHMS, load_models
from krx_quant.backfill import BAR_SCHEMA, FUTURES_BAR_SCHEMA, FUTURES_BARS_TR, MINUTE_BARS_TR
from krx_quant.cli import main
from krx_quant.feeds import KST
from krx_quant.reference import Bar
from tests.synthetic_bars import day_of_bars, random_walk, trending

DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]


def backfill(data_dir: Path, day: date, bars: list[Bar] | None = None, symbol: str = "005930") -> None:
    rows = [
        {
            "symbol": symbol,
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in (trending(day, seed=day.day) if bars is None else bars)
    ]
    path = data_dir / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / f"symbol={symbol}" / "bars.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def backfill_futures(data_dir: Path, day: date, bars: list[Bar], series: str = "KOSPI200F") -> None:
    rows = [
        {
            "symbol": series,
            "bar_time": b.start,
            "contract": "A01612",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in bars
    ]
    path = data_dir / "bars" / f"date={day}" / f"feed={FUTURES_BARS_TR}" / f"symbol={series}" / "bars.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=FUTURES_BAR_SCHEMA), path)


def led_by_futures(day: date, seed: int) -> tuple[list[Bar], list[Bar]]:
    """KOSPI200 futures moving at random, and a stock moving 3 ticks for every standard
    deviation the futures moved two minutes before, plus a tick of noise.
    """
    rng = random.Random(seed)
    moves = [rng.gauss(0, 1) for _ in range(390)]
    level, futures = 0.0, []
    for i, z in enumerate(moves):
        level += 0.0005 * z
        at = datetime.combine(day, time(9), KST) + timedelta(minutes=i)
        price = 1000 * math.exp(level)
        futures.append(Bar(at, price, price, price, price, 1, 1, at, at))
    steps = [3 * (moves[i - 2] if i >= 2 else 0.0) + rng.gauss(0, 1) for i in range(390)]
    return futures, day_of_bars(day, steps, seed=seed)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")
    return tmp_path


def test_fitting_newton_saves_a_model_the_engine_loads(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for day in DAYS:
        backfill(env / "data", day)

    assert main(["fit", "newton", "--days", "3"]) == 0

    model = load_models(env / "state", ALGORITHMS)["newton"]
    assert model.version.startswith("newton-20260918-")  # the latest three days
    assert model.trained is not None and model.trained.startswith("2026-09-16..2026-09-18")
    out = capsys.readouterr().out
    assert model.version in out
    assert "trained on 3 days" in out


def test_fitting_needs_a_known_algorithm_and_backfilled_bars(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fit", "einstein"]) == 1
    assert "unknown algorithm 'einstein'" in capsys.readouterr().err
    assert main(["fit", "newton"]) == 1
    assert "no backfilled bars" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["fit", "newton", "--days", "0"])
    assert "must be at least 1" in capsys.readouterr().err


def test_granger_trains_on_the_backfilled_futures_etfs_and_other_symbols(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KRX_SYMBOLS", "005930,000660")
    monkeypatch.setenv("KRX_ETF_SYMBOLS", "133690")
    for i, day in enumerate(DAYS):
        futures, led = led_by_futures(day, seed=i)
        backfill(env / "data", day, led)
        backfill(env / "data", day, random_walk(day, seed=10 + i), symbol="000660")
        backfill(env / "data", day, random_walk(day, seed=20 + i), symbol="133690")
        backfill_futures(env / "data", day, futures)

    assert main(["fit", "granger"]) == 0, capsys.readouterr().err

    params = load_models(env / "state", ALGORITHMS)["granger"].params
    # A stock that moved after the futures: their last two minutes are what it moves next on.
    assert params["lead_kospi200f_1"] > 0.1 and params["lead_kospi200f_2"] > 0.1, params
    assert params["lead_indices_1"] == params["lead_us_1"] == 0.0, "no backfill of either"
