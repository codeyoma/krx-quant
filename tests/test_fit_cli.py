"""``krx-quant fit <algorithm>``: train on backfilled bars, save the model the engine loads."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import ALGORITHMS, load_models
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.cli import main
from tests.synthetic_bars import trending

DAYS = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]


def backfill(data_dir: Path, day: date) -> None:
    rows = [
        {
            "symbol": "005930",
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in trending(day, seed=day.day)
    ]
    path = data_dir / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / "symbol=005930" / "bars.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


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
