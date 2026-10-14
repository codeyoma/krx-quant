"""Past bar closes' touch labels (#168): filled from the recorded day at start, and by hand.

Input: an engine database of the current schema (v4), as the server's was before #167: a
recorded day's predictions and outcomes without touch labels. And that day's events, recorded
by the collector's recorder (in the data directory, or archived to the Object Store). Output:
the ``touches`` rows, against the labels the Replay Harness gives the same day afresh, the
outcomes left as they were, and the Engine API of a collector started on that database.

The day is a wandering book and trades from 09:00 to 10:00 with a VI at 09:30 (every class,
VI bar closes, quotes gone silent at 10:00, then no mid-point), and the KOSPI index and its
futures, which the fill does not read: a circuit breaker at 09:41 marks predictions special.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import rescore as rescore_module
from krx_quant.archive import archive
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.fake_kis import FakeKis
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.rescore import EngineLock, EngineRunning, fill_touches, rescore
from krx_quant.touch import NOT_RECORDED, RESTARTED, SILENT, TouchResult
from tests.replay_events import DAY, SYMBOL, at, kospi, kospi200_futures, run
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll
from tests.test_replay import busy_morning
from tests.test_replay_cli import END_OF_DAY, record
from tests.test_special import VI, held

NEXT_MORNING = at("06:00:00", DAY + timedelta(days=1))  # the 06:00 deploy after the day
BAR_CLOSES = 376  # 09:01 to 15:16


def day_events() -> list[Event]:
    market = [
        *held("09:40:00", "09:42:00", kospi, -8.2),
        *held("09:20:00", "09:22:00", kospi200_futures, 1.0),
    ]
    return sorted([*busy_morning(), *VI, *market], key=lambda e: e[1]["received_at"])


def labels(db: Path) -> list[tuple[Any, ...]]:
    """Every touch label stored, with its path and when it was scored."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT symbol, bar_close, target_version, class, unscorable, path, scored_at FROM touches"
            " ORDER BY bar_close, symbol"
        ).fetchall()


def outcomes(db: Path) -> list[tuple[Any, ...]]:
    """Every outcome on bases A and B, with its special mark, by prediction."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT p.symbol, p.algorithm, p.bar_close, o.mid_close, o.mid_display, o.mid_target,"
            " o.flat_ticks, o.class_a, o.class_b, o.special, o.unscorable, o.scored_at"
            " FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
            " ORDER BY p.bar_close, p.symbol, p.algorithm"
        ).fetchall()


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The day replayed afresh through the Replay Harness: the labels to match."""
    db = tmp_path_factory.mktemp("replayed") / "replayed.sqlite3"
    asyncio.run(_replay(db))
    return db


async def _replay(db: Path) -> None:
    engine = await run(db, day_events(), until=END_OF_DAY)
    engine.store.close()


def server_db(state: Path, replayed: Path, *, keep: int | None = None) -> Path:
    """The live database as an engine before #167 left it (schema v4, no ``touches``); with
    ``keep``, as #167 left it after starting mid-day: only the labels of every ``keep``-th
    bar close."""
    state.mkdir(parents=True, exist_ok=True)
    db = state / DB_NAME
    with sqlite3.connect(replayed) as source, sqlite3.connect(db) as target:
        source.backup(target)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        if keep is None:
            conn.execute("DROP TABLE touches")
        else:
            rows = conn.execute("SELECT rowid FROM touches ORDER BY bar_close").fetchall()
            conn.executemany(
                "DELETE FROM touches WHERE rowid = ?", [r for i, r in enumerate(rows) if i % keep]
            )
    return db


def settings(tmp_path: Path, **changes: Any) -> Settings:
    return replace(
        Settings(
            kis_app_key="",
            kis_app_secret="",
            symbols=(SYMBOL,),
            etf_symbols=(),
            index_codes=(),
            us_symbols=(),
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
        ),
        **changes,
    )


def test_the_fill_labels_a_recorded_day_as_a_fresh_replay_does_and_once(
    tmp_path: Path, replayed: Path
) -> None:
    """Input: the server's database before #167 and the day recorded in the data directory.
    Output: one label per bar close, each the fresh replay's (its path and scored_at too);
    a second run writes nothing; the outcomes are as they were."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = server_db(s.state_dir, replayed)
    before = outcomes(db)

    first = fill_touches(s, NEXT_MORNING)

    assert [(f.day, f.source, f.labeled, f.written, f.not_recorded) for f in first] == [
        (DAY, "the data directory", BAR_CLOSES, BAR_CLOSES, 0)
    ]
    assert labels(db) == labels(replayed)
    assert {row[3] for row in labels(db)} >= {"up", "flat", "down"}  # the day has every class
    assert outcomes(db) == before

    assert fill_touches(s, NEXT_MORNING) == []  # nothing missing: no day read
    assert labels(db) == labels(replayed)
    with sqlite3.connect(db) as conn:
        runs = conn.execute("SELECT kind FROM runs WHERE kind = 'touch_fill'").fetchall()
    assert runs == [("touch_fill",)]  # the first run only


def test_labels_an_older_engine_wrote_stay_and_only_the_missing_are_filled(
    tmp_path: Path, replayed: Path
) -> None:
    """Input: a third of the bar closes labeled already, one of them differently (``restarted``,
    as an engine restarted in its window writes it). Output: that one stays; the others are
    the fresh replay's."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = server_db(s.state_dir, replayed, keep=3)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE touches SET class = NULL, unscorable = ? WHERE bar_close = ?",
            (RESTARTED, "2026-10-12T09:31:00+09:00"),
        )
        kept = len(conn.execute("SELECT 1 FROM touches").fetchall())

    [filled] = fill_touches(s, NEXT_MORNING)

    assert filled.written == BAR_CLOSES - kept
    expected = [
        (*row[:3], None, RESTARTED, *row[5:]) if row[1] == "2026-10-12T09:31:00+09:00" else row
        for row in labels(replayed)
    ]
    assert labels(db) == expected


def test_a_day_no_longer_in_the_data_directory_is_read_from_the_object_store(
    tmp_path: Path, replayed: Path
) -> None:
    """Input: the day recorded elsewhere and archived to the Object Store (the data directory
    lacks it). Output: the fresh replay's labels."""
    elsewhere = tmp_path / "elsewhere"
    record(elsewhere, day_events())
    asyncio.run(archive(elsewhere, LocalObjectStore(tmp_path / "store")))
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = server_db(s.state_dir, replayed)

    [filled] = fill_touches(s, NEXT_MORNING)

    assert filled.source == str((tmp_path / "store").resolve())
    assert labels(db) == labels(replayed)


def test_a_day_recorded_nowhere_is_left_unscorable_and_logged(
    tmp_path: Path, replayed: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: neither the data directory nor the Object Store has the day. Output: every
    predicted bar close ``not recorded``, a warning; nothing more on a second run."""
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = server_db(s.state_dir, replayed)

    with caplog.at_level(logging.WARNING, logger="krx_quant.rescore"):
        [filled] = fill_touches(s, NEXT_MORNING)

    assert (filled.labeled, filled.written, filled.not_recorded) == (0, BAR_CLOSES, BAR_CLOSES)
    assert {(row[3], row[4]) for row in labels(db)} == {(None, NOT_RECORDED)}
    assert f"{DAY}: {BAR_CLOSES} predicted bar closes of {SYMBOL} have no recorded events" in caplog.text
    assert fill_touches(s, NEXT_MORNING) == []


class _Unreachable(LocalObjectStore):
    def keys(self, prefix: str = "") -> list[str]:
        raise OSError("the store is out of reach")


def test_a_day_that_cannot_be_read_now_is_tried_again_next_time(
    tmp_path: Path, replayed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: the Object Store fails while the day is read from it. Output: nothing written
    (not ``not recorded``); the next run, with the store back, fills the day."""
    elsewhere = tmp_path / "elsewhere"
    record(elsewhere, day_events())
    asyncio.run(archive(elsewhere, LocalObjectStore(tmp_path / "store")))
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = server_db(s.state_dir, replayed)
    monkeypatch.setattr(rescore_module, "open_store", lambda _: _Unreachable(tmp_path / "store"))

    [failed] = fill_touches(s, NEXT_MORNING)

    assert failed.failed == "OSError: the store is out of reach"
    assert labels(db) == []
    monkeypatch.undo()
    fill_touches(s, NEXT_MORNING)
    assert labels(db) == labels(replayed)


def test_today_is_left_to_the_live_engine(tmp_path: Path, replayed: Path) -> None:
    """Input: the fill runs on the day itself (the engine restarted at noon). Output: nothing:
    the live engine labels its windows, those of a restart included."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = server_db(s.state_dir, replayed)

    assert fill_touches(s, at("12:00:00")) == []
    assert labels(db) == []


def test_rescore_computes_every_label_again_and_leaves_the_outcomes(
    tmp_path: Path, replayed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Input: labels stored, two of them wrong; outcomes with special marks (the VI's and the
    circuit breaker's). Output of ``krx-quant rescore``: the fresh replay's labels, the
    outcomes (classes A and B, special marks) as they were; running it again writes nothing."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = server_db(s.state_dir, replayed, keep=1)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE touches SET class = 'down' WHERE bar_close IN (?, ?)",
            ("2026-10-12T09:41:00+09:00", "2026-10-12T09:42:00+09:00"),
        )
    before = outcomes(db)
    assert any(row[9] for row in before)  # special marks to keep: the VI's and the breaker's
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", SYMBOL)

    assert main(["rescore"]) == 0

    assert labels(db) == labels(replayed)
    assert outcomes(db) == before
    assert f"{DAY}: {BAR_CLOSES} windows from the data directory, 2 rows written" in capsys.readouterr().out

    assert main(["rescore"]) == 0
    assert f"{DAY}: {BAR_CLOSES} windows from the data directory, 0 rows written" in capsys.readouterr().out
    assert labels(db) == labels(replayed)


def test_rescore_takes_today_once_its_windows_are_over(tmp_path: Path, replayed: Path) -> None:
    """Input: rescore on the day at 15:19 and at 15:20 (the last window's end). Output: the day
    left alone, then labeled."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = server_db(s.state_dir, replayed)

    assert rescore(s, at("15:19:59")) == []
    assert [f.written for f in rescore(s, at("15:20:00"))] == [BAR_CLOSES]
    assert labels(db) == labels(replayed)


def test_rescore_keeps_the_labels_of_a_day_it_cannot_find(tmp_path: Path, replayed: Path) -> None:
    """Input: labels stored (by the live engine), the day's recording gone from the data
    directory, no Object Store. Output: the labels as they were."""
    s = settings(tmp_path)
    db = server_db(s.state_dir, replayed, keep=1)

    [filled] = rescore(s, NEXT_MORNING)

    assert (filled.source, filled.labeled, filled.written, filled.not_recorded) == (None, 0, 0, 0)
    assert labels(db) == labels(replayed)


def test_rescore_is_refused_while_the_engine_holds_its_lock(tmp_path: Path) -> None:
    """Input: the engine lock held (as the running engine holds it). Output: rescore refuses;
    once the lock is let go, it runs."""
    held = EngineLock(tmp_path / "state")
    assert held.acquire()
    try:
        with pytest.raises(EngineRunning, match="the engine is running"):
            rescore(settings(tmp_path), NEXT_MORNING)
    finally:
        held.release()
    assert rescore(settings(tmp_path), NEXT_MORNING) == []  # free again


async def test_the_engine_answers_first_then_shows_the_filled_labels(
    kis: FakeKis,
    tmp_path: Path,
    replayed: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Input: the collector started at the 06:00 deploy on the server's database before #167,
    the day recorded in its data directory, while a rescore still holds the lock. Output: the
    Engine API answers without the labels; once the rescore is done, the engine fills them
    and the API shows them. While it runs, ``krx-quant rescore`` is refused."""
    port = free_port()
    s = replace(settings_for(kis, tmp_path), api_port=port)
    record(s.data_dir, day_events())
    server_db(s.state_dir, replayed)
    monkeypatch.setattr(rescore_module, "LOCK_POLL_SECONDS", 0.05)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    rescoring = EngineLock(s.state_dir)
    assert rescoring.acquire()
    base = f"http://127.0.0.1:{port}/v1"
    url = f"{base}/symbols/{SYMBOL}/predictions?bar_close=2026-10-12T09:45:00%2B09:00&algorithm=baseline-flat"
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(s, stop=stop, clock=FakeClock(NEXT_MORNING)))
    try:
        await poll(f"{base}/status", lambda b: True)
        waiting = await poll(url, lambda b: True)
        rescoring.release()
        filled = await poll(url, lambda b: b["predictions"][0]["outcome"]["touch"] is not None)
        refused = main(["rescore"])
    finally:
        rescoring.release()
        stop.set()
        await asyncio.wait_for(task, 5)

    assert waiting["predictions"][0]["outcome"]["touch"] is None
    expected = next(r for r in labels(replayed) if r[1] == "2026-10-12T09:45:00+09:00")
    touch = filled["predictions"][0]["outcome"]["touch"]
    assert (touch["target_version"], touch["class"], touch["unscorable"]) == expected[2:5]
    assert labels(s.state_dir / DB_NAME) == labels(replayed)
    assert refused == 1
    assert "the engine is running" in capsys.readouterr().err


def test_the_lock_is_free_once_its_holder_lets_go(tmp_path: Path) -> None:
    """Input: two holders of the engine lock in turn. Output: one at a time."""
    first, second = EngineLock(tmp_path), EngineLock(tmp_path)
    assert first.acquire() and first.acquire()  # held already: still held
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_storing_labels_that_change_nothing_leaves_the_store_version(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / DB_NAME)
    try:
        labels = [TouchResult(SYMBOL, at("09:31:00"), None, SILENT)]
        assert store.add_touches(labels) == 1
        version = store.version
        # Stored already: neither a fill nor a rescore writes, so the cached statistics stay.
        assert store.add_touches(labels) == 0
        assert store.add_touches(labels, replace=True) == 0
        assert store.version == version
        assert store.add_touches([TouchResult(SYMBOL, at("09:31:00"), None, RESTARTED)], replace=True) == 1
        assert store.version == version + 1
    finally:
        store.close()
