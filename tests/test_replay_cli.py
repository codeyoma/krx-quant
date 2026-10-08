"""``krx-quant replay <day>``: a recorded day through the engine.

Input: events written by the collector's recorder, partly merged by the archive. Output:
the replay database, which must match replaying the same events by hand.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from krx_quant.archive import archive
from krx_quant.cli import main
from krx_quant.engine import Event
from krx_quant.feeds import WS_FEEDS
from krx_quant.object_store import LocalObjectStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.replay import recorded_events
from tests.replay_events import DAY, at, run
from tests.test_replay import busy_morning, stored

END_OF_DAY = at("23:59:59.999999")


def record(data_dir: Path, events: list[Event]) -> None:
    recorder = ParquetRecorder(data_dir, flush_rows=50, flush_seconds=3600)
    for feed, row in events:
        recorder.record("events", DAY, feed, row["symbol"], row, WS_FEEDS[feed].schema)
    recorder.close()


async def test_a_recorded_day_replays_like_the_same_events_given_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    events = busy_morning()
    half = len(events) // 2
    record(data, events[:half])
    merged_parts = {p: p.read_bytes() for p in data.rglob("part-*.parquet")}
    await archive(data, LocalObjectStore(tmp_path / "archive"))
    # Parts a crash left behind after the merge: already in data.parquet, never read twice.
    for path, content in merged_parts.items():
        path.write_bytes(content)
    record(data, events[half:])  # rows recorded after the merge
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    # Every recorded row once, none of the leftover parts twice.
    assert len(list(recorded_events(data, DAY, ["005930"]))) == len(events)
    assert main(["replay", DAY.isoformat()]) == 0

    db = tmp_path / "state" / f"replay-{DAY}.sqlite3"
    run(tmp_path / "by-hand.db", events, until=END_OF_DAY)
    assert stored(db) == stored(tmp_path / "by-hand.db")
    out = capsys.readouterr().out
    assert f"replayed {DAY} into {db}" in out
    assert "baseline-flat: 376 predictions" in out


@pytest.mark.parametrize("name", ["engine.sqlite3", "engine.sqlite3-wal", "engine.sqlite3-shm"])
def test_replay_never_replaces_the_live_engine_database(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record(tmp_path / "data", busy_morning())
    state = tmp_path / "state"
    state.mkdir()
    live = {f: state / f for f in ("engine.sqlite3", "engine.sqlite3-wal", "engine.sqlite3-shm")}
    for path in live.values():
        path.write_bytes(b"live predictions")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))

    assert main(["replay", DAY.isoformat(), "--db", f"state/{name}"]) == 1

    assert all(path.read_bytes() == b"live predictions" for path in live.values())
    assert "live engine's database" in capsys.readouterr().err


def test_a_row_recorded_after_the_system_clock_stepped_back_still_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = busy_morning()
    # The clock stepped back 0.4 s: a book is received "before" the book written before it.
    books = [i for i, (feed, _) in enumerate(events) if feed == "H0UNASP0"]
    feed, row = events[books[10]]
    events[books[10]] = (
        feed,
        {**row, "received_at": events[books[9]][1]["received_at"] - timedelta(seconds=0.4)},
    )
    record(tmp_path / "data", events)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert main(["replay", DAY.isoformat()]) == 0
    assert "baseline-flat: 376 predictions" in capsys.readouterr().out


def test_replaying_a_day_with_nothing_recorded_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))

    assert main(["replay", DAY.isoformat()]) == 1
    assert "no events" in capsys.readouterr().err
