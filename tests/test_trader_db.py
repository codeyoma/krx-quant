"""The trader's store as its modules read it (#509, ``trader/db.py``): a read-only open of it, None
without it, its rows by column name, closed once the read is done; and the times it keeps, ISO 8601
in KST.

Input: a small store written in write-ahead-log mode as the trader keeps its own. Every value is
made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from krx_quant.feeds.base import KST
from krx_quant.trader.db import iso, reading


def write(path: Path) -> sqlite3.Connection:
    """A store with one row in ``runs``, open for writing in WAL mode as the trader keeps it."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, at TEXT NOT NULL, mode TEXT NOT NULL)")
    db.execute("INSERT INTO runs (at, mode) VALUES (?, ?)", ("2026-11-04T08:30:00+09:00", "paper"))
    db.commit()
    return db


def test_without_the_store_it_gives_none_and_makes_no_file(tmp_path: Path) -> None:
    path = tmp_path / "trader.sqlite3"
    with reading(path) as db:
        assert db is None
    assert list(tmp_path.iterdir()) == []


def test_a_write_through_it_fails(tmp_path: Path) -> None:
    path = tmp_path / "trader.sqlite3"
    with closing(write(path)):
        with reading(path) as db:
            assert db is not None
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("INSERT INTO runs (at, mode) VALUES (?, ?)", ("2026-11-04T09:00:00+09:00", "mock"))
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("CREATE TABLE other (id INTEGER)")
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT mode FROM runs").fetchall() == [("paper",)]


def test_its_rows_are_read_by_column_name_beside_the_writer(tmp_path: Path) -> None:
    path = tmp_path / "trader.sqlite3"
    with closing(write(path)) as writer:
        with reading(path) as db:
            assert db is not None
            row = db.execute("SELECT * FROM runs").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row.keys() == ["id", "at", "mode"]
        assert (row["id"], row["at"], row["mode"]) == (1, "2026-11-04T08:30:00+09:00", "paper")
        assert dict(row) == {"id": 1, "at": "2026-11-04T08:30:00+09:00", "mode": "paper"}
        # The writer stays open, as the trader does: a later read sees each commit as it is made.
        writer.execute("INSERT INTO runs (at, mode) VALUES (?, ?)", ("2026-11-04T09:00:00+09:00", "mock"))
        writer.commit()
        with reading(path) as db:
            assert db is not None
            assert [r["mode"] for r in db.execute("SELECT mode FROM runs ORDER BY id")] == ["paper", "mock"]


def test_it_is_closed_once_the_read_is_done(tmp_path: Path) -> None:
    path = tmp_path / "trader.sqlite3"
    with closing(write(path)):
        with reading(path) as db:
            assert db is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_it_is_closed_when_the_read_fails(tmp_path: Path) -> None:
    path = tmp_path / "trader.sqlite3"
    with closing(write(path)):
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with reading(path) as db:
                assert db is not None
                db.execute("SELECT * FROM missing")
        assert db is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_a_time_is_kept_as_iso_8601_in_kst() -> None:
    assert iso(datetime(2026, 11, 4, 0, 30, tzinfo=UTC)) == "2026-11-04T09:30:00+09:00"
    assert iso(datetime(2026, 11, 4, 9, 30, 0, 500_000, tzinfo=KST)) == "2026-11-04T09:30:00.500000+09:00"
    # A UTC evening is the next KST day: the strings sort as the times do.
    late, early = datetime(2026, 11, 4, 16, 0, tzinfo=UTC), datetime(2026, 11, 5, 0, 30, tzinfo=KST)
    assert early < late and iso(early) < iso(late)
