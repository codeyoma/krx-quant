"""The prediction database written by an earlier engine opens in this one, data kept."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from krx_quant.baselines import Forecast
from krx_quant.feeds import KST
from krx_quant.predictions import NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction

# The schema the first engine (#9) wrote, verbatim.
SCHEMA_V1 = """
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, algorithm TEXT NOT NULL,
    model_version TEXT NOT NULL, bar_close TEXT NOT NULL, displayed_at TEXT NOT NULL,
    p_up REAL NOT NULL, p_flat REAL NOT NULL, p_down REAL NOT NULL,
    expected_ticks REAL NOT NULL, status TEXT NOT NULL, UNIQUE (symbol, algorithm, bar_close)
);
CREATE INDEX predictions_by_time ON predictions (symbol, bar_close);
CREATE TABLE outcomes (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id), target_at TEXT NOT NULL,
    mid_close REAL, mid_display REAL, mid_target REAL, flat_ticks INTEGER, class_a TEXT,
    class_b TEXT, special INTEGER NOT NULL DEFAULT 0, unscorable TEXT, scored_at TEXT
);
CREATE INDEX outcomes_pending ON outcomes (scored_at, target_at);
PRAGMA user_version=1;
INSERT INTO predictions VALUES (1, '005930', 'baseline-flat', 'v1', '2026-10-12T09:31:00+09:00',
    '2026-10-12T09:31:00+09:00', 0, 1, 0, 0, 'ok');
INSERT INTO outcomes VALUES (1, '2026-10-12T09:35:00+09:00', 81250, 81250, NULL, 2, NULL, NULL, 0,
    NULL, NULL);
"""

T = datetime(2026, 10, 12, 9, 32, tzinfo=KST)


def test_a_database_of_the_first_engine_is_upgraded_in_place(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA_V1)

    store = PredictionStore(db)
    [pending] = store.pending()
    store.add_all(
        [
            NewPrediction("005930", "newton", "newton-default", T, T, None, T, None, None, None, "timed_out"),
            NewPrediction("005930", "baseline-flat", "v1", T, T, Forecast(0, 1, 0, 0), T, 81250, 81250, 2),
        ]
    )

    assert pending.prediction_id == 1 and pending.mid_close == 81250
    # The first engine's outcomes already had the special column: marking one works.
    store.score_all([(1, Score(81550, Direction.UP, Direction.UP, None))], T)
    store.mark_special("005930", T, T + timedelta(minutes=1), timedelta(minutes=4))
    [special] = store.scored_predictions([T.date()], "005930", include_special=True)
    assert (special["class_a"], special["special"]) == ("up", 1)
    assert store.scored_predictions([T.date()], "005930", include_special=False) == []
    [kept] = store.recent("005930", 10, "baseline-flat")[1:]
    assert kept["bar_close"] == "2026-10-12T09:31:00+09:00"
    [skipped] = store.latest("005930")[1:]
    assert (skipped["algorithm"], skipped["status"], skipped["p_up"]) == ("newton", "timed_out", None)
    # Later versions added tables: algorithm statuses (3).
    store.add_status("newton", T, None, "probation", "registered")
    assert store.statuses() == {"newton": ("probation", T)}
    store.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
