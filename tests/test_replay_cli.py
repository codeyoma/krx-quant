"""``krx-quant replay <day>``: a recorded day through the engine.

Input: events written by the collector's recorder, partly merged by the archive. Output:
the replay database, which must match replaying the same events by hand.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from krx_quant.algorithms import Model, save_model
from krx_quant.archive import archive
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.feeds import INDEX_REST, VI_STATUS, WS_FEEDS
from krx_quant.lifecycle import Rules
from krx_quant.object_store import LocalObjectStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.replay import recorded_events, replay_recorded
from tests.replay_events import DAY, at, kospi, kospi200_futures, run
from tests.test_replay import busy_morning, stored
from tests.test_special import VI, held

END_OF_DAY = at("23:59:59.999999")
SCHEMAS = {
    **{f: feed.schema for f, feed in WS_FEEDS.items()},
    **{f.tr_id: f.schema for f in (VI_STATUS, INDEX_REST)},
}


def record(
    data_dir: Path, events: list[Event], schemas: dict[str, pa.Schema] = SCHEMAS, *, day: date = DAY
) -> None:
    recorder = ParquetRecorder(data_dir, flush_rows=50, flush_seconds=3600)
    for feed, row in events:
        recorder.record("events", day, feed, row["symbol"], row, schemas[feed])
    recorder.close()


def test_a_recorded_day_replays_like_the_same_events_given_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    events = busy_morning()
    half = len(events) // 2
    record(data, events[:half])
    merged_parts = {p: p.read_bytes() for p in data.rglob("part-*.parquet")}
    asyncio.run(archive(data, LocalObjectStore(tmp_path / "archive")))
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
    asyncio.run(run(tmp_path / "by-hand.db", events, until=END_OF_DAY))
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


def test_replay_does_not_use_a_model_trained_on_the_day_it_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record(tmp_path / "data", busy_morning())
    state = tmp_path / "state"
    save_model(
        state, "newton", Model("newton-20261012-aaaaaa", {"damping": 0.0, "threshold": 0.0}, None, DAY)
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(state))

    assert main(["replay", DAY.isoformat()]) == 0

    with sqlite3.connect(state / f"replay-{DAY}.sqlite3") as conn:
        versions = conn.execute("SELECT DISTINCT model_version FROM predictions WHERE algorithm = 'newton'")
        assert versions.fetchall() == [("newton-default",)]
    assert "newton-20261012-aaaaaa was trained on data up to 2026-10-12" in capsys.readouterr().err


def replays_like_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Event], recorded: int | None = None
) -> None:
    data = tmp_path / "data"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(data))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert len(list(recorded_events(data, DAY, ["005930"]))) == (recorded or len(events))
    assert main(["replay", DAY.isoformat()]) == 0

    asyncio.run(run(tmp_path / "by-hand.db", events, until=END_OF_DAY))
    assert stored(tmp_path / "state" / f"replay-{DAY}.sqlite3") == stored(tmp_path / "by-hand.db")


def test_vis_breakers_and_sidecars_replay_from_the_recorded_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = [
        *busy_morning(),
        *VI,
        *held("09:40:00", "09:42:00", kospi, -8.2),
        *held("09:20:00", "09:22:00", kospi200_futures, 5.3),
    ]
    record(tmp_path / "data", events)

    replays_like_by_hand(tmp_path, monkeypatch, events)
    with sqlite3.connect(tmp_path / "state" / f"replay-{DAY}.sqlite3") as conn:
        [(special,)] = conn.execute("SELECT COUNT(*) FROM outcomes WHERE special = 1").fetchall()
        [(vi,)] = conn.execute("SELECT COUNT(*) FROM predictions WHERE status = 'vi'").fetchall()
    assert special > 0 and vi > 0


def test_futures_recorded_before_their_change_was_kept_still_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Days recorded before #12 have no change_pct in their futures events: no sidecar is called.
    futures = held("09:20:00", "09:22:00", kospi200_futures, 5.3)
    old = WS_FEEDS["H0IFCNT0"].schema.remove(WS_FEEDS["H0IFCNT0"].schema.get_field_index("change_pct"))
    record(tmp_path / "data", busy_morning())
    record(
        tmp_path / "data",
        [(f, {k: v for k, v in row.items() if k != "change_pct"}) for f, row in futures],
        {**SCHEMAS, "H0IFCNT0": old},
    )

    replays_like_by_hand(tmp_path, monkeypatch, busy_morning(), recorded=len(busy_morning()) + len(futures))


async def test_a_recorded_replay_judges_statuses_by_the_server_s_rules(tmp_path: Path) -> None:
    env = {
        "KRX_STATE_DIR": str(tmp_path / "state"),
        "KRX_PROBATION_REVIEW_DAYS": "1,5",
        "KRX_REVIEW_CONFIDENCE": "0.99",
    }
    settings = Settings.from_env(env, dotenv_path=None, require_kis=False)

    engine = await replay_recorded(settings, DAY, [], db=tmp_path / "replay.db", models={})

    assert engine.review_rules == Rules((1, 5), (20, 40, 60), 0.99)
