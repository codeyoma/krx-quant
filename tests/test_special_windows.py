"""Special situations' windows kept (#236): the engine keeps every window it learns, fills
past days' from their recordings at start, and answers them by symbol and days.

Input: hand-made events (a wandering book and trades, VI status answers, the KOSPI index and
KOSPI200 futures) through the Replay Harness, and the same day recorded by the collector's
recorder (in the data directory, or archived to the Object Store). Output: the
``special_windows`` and ``special_days`` rows, against those a fresh replay of the day keeps,
and ``GET /v1/symbols/{symbol}/special`` within the contract.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import rescore as rescore_module
from krx_quant.archive import archive
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.engine import Event
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.replay import replay_into
from krx_quant.rescore import EngineLock, fill_special_windows
from krx_quant.special import Kind, Window
from tests.api_schema import check
from tests.replay_events import DAY, SYMBOL, api, at, get_json, kospi, kospi200_futures, run, vi
from tests.test_collector import settings_for
from tests.test_live_predictions import free_port, poll
from tests.test_replay import busy_morning
from tests.test_replay_cli import record
from tests.test_rescore import NEXT_MORNING, settings
from tests.test_special import held

OTHER = "000660"
# A sidecar at 09:21 (futures 5.3% up from 09:20), a VI of the symbol from 09:30:20 seen on at
# 09:30:24 and released at 09:32:40 (seen at 09:32:44), and a circuit breaker's first stage at
# 09:41 (KOSPI 8.2% down from 09:40) and second at 10:21 (15.4% down from 10:20).
MARKET = [
    *held("09:20:00", "09:22:00", kospi200_futures, 5.3),
    *held("09:40:00", "10:19:55", kospi, -8.2),
    *held("10:20:00", "10:22:00", kospi, -15.4),
]
VI_ON = vi(at("09:30:24"), "09:30:20")
VI_RELEASED = vi(at("09:32:44"), "09:30:20", "09:32:40")
# The windows kept: kind, symbol, start, end, detail, learned_at.
KEPT = [
    (
        "sidecar",
        None,
        "2026-10-12T09:21:00+09:00",
        "2026-10-12T09:26:00+09:00",
        None,
        "2026-10-12T09:21:00+09:00",
    ),
    (
        "vi",
        SYMBOL,
        "2026-10-12T09:30:20+09:00",
        "2026-10-12T09:32:40+09:00",
        "dynamic",
        "2026-10-12T09:32:44+09:00",
    ),
    (
        "circuit_breaker",
        None,
        "2026-10-12T09:41:00+09:00",
        "2026-10-12T10:11:00+09:00",
        "1단계",
        "2026-10-12T09:41:00+09:00",
    ),
    (
        "circuit_breaker",
        None,
        "2026-10-12T10:21:00+09:00",
        "2026-10-12T10:51:00+09:00",
        "2단계",
        "2026-10-12T10:21:00+09:00",
    ),
]


def day_events() -> list[Event]:
    return sorted([*busy_morning(), VI_ON, VI_RELEASED, *MARKET], key=lambda e: e[1]["received_at"])


def kept(db: Path) -> list[tuple[Any, ...]]:
    """Every special window kept, by start."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT kind, symbol, start, end, detail, learned_at FROM special_windows ORDER BY start, kind"
        ).fetchall()


def decided(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT day, source, windows, decided_at FROM special_days ORDER BY day"
        ).fetchall()


def runs(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT kind FROM runs WHERE kind = 'special_fill'").fetchall()


# --- Kept as learned live.


async def test_the_engine_keeps_each_window_it_learns_and_a_vis_release_over_it(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    events = day_events()
    before_release = [e for e in events if e[1]["received_at"] <= at("09:31:00")]

    engine = await run(db, before_release, until=at("09:31:00"))
    on = kept(db)
    clock = engine.clock
    assert isinstance(clock, FakeClock)
    await replay_into(engine, clock, events[len(before_release) :], until=at("11:00:00"))

    # Still on at 09:31: three minutes from its start, as the engine takes it.
    assert on == [
        KEPT[0],
        (
            "vi",
            SYMBOL,
            "2026-10-12T09:30:20+09:00",
            "2026-10-12T09:33:20+09:00",
            "dynamic",
            "2026-10-12T09:30:24+09:00",
        ),
    ]
    assert kept(db) == KEPT, "one row per window: the VI's release over it"


def test_a_database_from_before_gets_the_tables_and_keeps_its_schema_version(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    PredictionStore(db).close()
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE special_windows")
        conn.execute("DROP TABLE special_days")

    store = PredictionStore(db)
    store.keep_special(Window(Kind.SIDECAR, None, at("09:21:00"), at("09:26:00")), at("09:21:00"))
    store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (4,), "an engine from before still opens it"
    assert kept(db) == [KEPT[0]]


# --- Filled from past days' recordings.


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The day replayed afresh through the Replay Harness: the windows to match."""
    db = tmp_path_factory.mktemp("replayed") / "replayed.sqlite3"
    asyncio.run(_replay(db))
    return db


async def _replay(db: Path) -> None:
    engine = await run(db, day_events(), until=at("11:00:00"))
    engine.store.close()


def before_236(state: Path, replayed: Path) -> Path:
    """The live database as an engine before #236 left it: the day predicted, no window kept."""
    state.mkdir(parents=True, exist_ok=True)
    db = state / DB_NAME
    with sqlite3.connect(replayed) as source, sqlite3.connect(db) as target:
        source.backup(target)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE special_windows")
        conn.execute("DROP TABLE special_days")
    return db


def test_the_fill_keeps_a_recorded_days_windows_as_a_fresh_replay_does_and_once(
    tmp_path: Path, replayed: Path
) -> None:
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = before_236(s.state_dir, replayed)

    first = fill_special_windows(s, NEXT_MORNING)

    assert [(f.day, f.source, f.labeled, f.written, f.failed) for f in first] == [
        (DAY, "the data directory", 4, 4, None)
    ]
    assert kept(replayed) == KEPT
    assert kept(db) == kept(replayed)
    assert decided(db) == [(DAY.isoformat(), "the data directory", 4, "2026-10-13T06:00:00+09:00")]
    assert fill_special_windows(s, NEXT_MORNING) == [], "nothing left: no day read"
    assert kept(db) == KEPT
    assert runs(db) == [("special_fill",)], "the first run only"


def test_the_fill_reads_a_day_no_longer_in_the_data_directory_from_the_object_store(
    tmp_path: Path, replayed: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    record(elsewhere, day_events())
    asyncio.run(archive(elsewhere, LocalObjectStore(tmp_path / "store")))
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = before_236(s.state_dir, replayed)

    [filled] = fill_special_windows(s, NEXT_MORNING)

    assert filled.source == str((tmp_path / "store").resolve())
    assert kept(db) == KEPT


def test_a_quiet_day_is_read_once(tmp_path: Path, replayed: Path) -> None:
    """Input: the day recorded with the KOSPI 2% down and no VI. Output: no window, the day
    decided; the next start reads nothing."""
    s = settings(tmp_path)
    record(s.data_dir, [*busy_morning(), *held("09:00:00", "10:00:00", kospi, -2.0)])
    db = before_236(s.state_dir, replayed)

    [filled] = fill_special_windows(s, NEXT_MORNING)

    assert (filled.source, filled.labeled, filled.written) == ("the data directory", 0, 0)
    assert kept(db) == []
    assert decided(db) == [(DAY.isoformat(), "the data directory", 0, "2026-10-13T06:00:00+09:00")]
    assert fill_special_windows(s, NEXT_MORNING) == []


def test_a_day_recorded_nowhere_is_decided_without_windows_and_logged(
    tmp_path: Path, replayed: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = before_236(s.state_dir, replayed)

    with caplog.at_level(logging.WARNING, logger="krx_quant.rescore"):
        [filled] = fill_special_windows(s, NEXT_MORNING)

    assert (filled.source, filled.labeled, filled.written) == (None, 0, 0)
    assert decided(db) == [(DAY.isoformat(), None, 0, "2026-10-13T06:00:00+09:00")]
    assert f"{DAY}: no VI status, KOSPI index nor KOSPI200 futures recorded in the data directory nor" in (
        caplog.text
    )
    assert fill_special_windows(s, NEXT_MORNING) == []


class _Unreachable(LocalObjectStore):
    def keys(self, prefix: str = "") -> list[str]:
        raise OSError("the store is out of reach")


def test_a_day_that_cannot_be_read_now_is_tried_again_next_time(
    tmp_path: Path, replayed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    record(elsewhere, day_events())
    asyncio.run(archive(elsewhere, LocalObjectStore(tmp_path / "store")))
    s = settings(tmp_path, object_store=str(tmp_path / "store"))
    db = before_236(s.state_dir, replayed)
    monkeypatch.setattr(rescore_module, "open_store", lambda _: _Unreachable(tmp_path / "store"))

    [failed] = fill_special_windows(s, NEXT_MORNING)

    assert failed.failed == "OSError: the store is out of reach"
    assert (kept(db), decided(db), runs(db)) == ([], [], [])
    monkeypatch.undo()
    fill_special_windows(s, NEXT_MORNING)
    assert kept(db) == KEPT


def test_a_past_day_with_part_of_its_windows_kept_live_is_replayed_whole(
    tmp_path: Path, replayed: Path
) -> None:
    """Input: the day's windows as an engine restarted at noon kept them: only the later ones,
    one with a wrong start (the special situations live in memory). Output: the next morning's
    fill replays the whole day and its windows replace those, as a fresh replay keeps them."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    s.state_dir.mkdir(parents=True)
    db = s.state_dir / DB_NAME
    with sqlite3.connect(replayed) as source, sqlite3.connect(db) as target:
        source.backup(target)
    with sqlite3.connect(db) as conn:
        first = conn.execute(
            "SELECT kind, symbol, start FROM special_windows ORDER BY start LIMIT 1"
        ).fetchone()
        conn.execute(
            "DELETE FROM special_windows WHERE kind = ? AND ifnull(symbol, '') = ifnull(?, '') AND start = ?",
            first,
        )
        conn.execute(
            "UPDATE special_windows SET start = '2026-10-12T12:00:00+09:00' WHERE start ="
            " (SELECT max(start) FROM special_windows)"
        )
        conn.execute("DELETE FROM special_days")
    assert kept(db) != KEPT

    [filled] = fill_special_windows(s, NEXT_MORNING)

    assert (filled.source, filled.failed) == ("the data directory", None)
    assert kept(db) == KEPT
    assert fill_special_windows(s, NEXT_MORNING) == []


def test_a_past_day_recorded_nowhere_keeps_its_live_windows(tmp_path: Path, replayed: Path) -> None:
    s = settings(tmp_path)
    s.state_dir.mkdir(parents=True)
    db = s.state_dir / DB_NAME
    with sqlite3.connect(replayed) as source, sqlite3.connect(db) as target:
        source.backup(target)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM special_days")

    [filled] = fill_special_windows(s, NEXT_MORNING)

    assert filled.source is None
    assert kept(db) == KEPT, "nothing recorded to replace them with"


def test_today_is_left_to_the_live_engine(tmp_path: Path, replayed: Path) -> None:
    """Input: the database before #236 filled on the day itself (the engine restarted at noon).
    Output: nothing read: the live engine keeps today's windows."""
    s = settings(tmp_path)
    record(s.data_dir, day_events())
    db = before_236(tmp_path / "noon", replayed)
    assert fill_special_windows(replace(s, state_dir=tmp_path / "noon"), at("12:00:00")) == []
    assert (kept(db), decided(db)) == ([], [])


async def test_the_engine_answers_first_then_fills_the_special_windows(
    kis: FakeKis, tmp_path: Path, replayed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: the collector started at the 06:00 deploy on the database before #236, the day
    recorded in its data directory, while a rescore holds the lock. Output: the Engine API
    answers with no window kept; once the lock is free, the engine fills them and answers them."""
    port = free_port()
    s = replace(settings_for(kis, tmp_path), api_port=port)
    record(s.data_dir, day_events())
    db = before_236(s.state_dir, replayed)
    monkeypatch.setattr(rescore_module, "LOCK_POLL_SECONDS", 0.05)
    rescoring = EngineLock(s.state_dir)
    assert rescoring.acquire()
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(s, stop=stop, clock=FakeClock(NEXT_MORNING)))
    url = f"http://127.0.0.1:{port}/v1/symbols/{SYMBOL}/special"
    try:
        waiting = await poll(url, lambda b: True)
        rescoring.release()
        filled = await poll(url, lambda b: len(b["special"]) == len(KEPT), timeout=30)
    finally:
        rescoring.release()
        stop.set()
        await asyncio.wait_for(task, 5)

    assert waiting["special"] == []
    check("special", filled)
    assert kept(db) == KEPT


# --- GET /v1/symbols/{symbol}/special.


async def two_symbols(tmp_path: Path) -> Any:
    """The day replayed with a second symbol, in a VI of its own from 09:50:00 to 09:52:10;
    a window of each of three days before kept by hand: a VI of the symbol on the 8th, a
    sidecar on the 6th and a VI of the other symbol on 14 August."""
    other = [
        vi(at("09:50:04"), "09:50:00", symbol=OTHER),
        vi(at("09:52:14"), "09:50:00", "09:52:10", symbol=OTHER),
    ]
    engine = await run(
        tmp_path / "engine.db", [*day_events(), *other], until=at("11:00:00"), symbols=(SYMBOL, OTHER)
    )
    for kind, symbol, days_before, start, end in (
        (Kind.VI, SYMBOL, 4, "10:00:00", "10:02:10"),
        (Kind.SIDECAR, None, 6, "09:30:00", "09:35:00"),
        (Kind.VI, OTHER, 59, "13:00:00", "13:02:00"),
    ):
        day = DAY - timedelta(days=days_before)
        engine.store.keep_special(Window(kind, symbol, at(start, day), at(end, day)), at(start, day))
    return engine


def starts(body: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    return [(w["kind"], w["symbol"], w["start"][:16]) for w in body["special"]]


async def test_a_symbols_windows_are_its_vis_and_the_whole_markets_by_start(tmp_path: Path) -> None:
    engine = await two_symbols(tmp_path)

    async with api(engine) as client:
        mine = await get_json(client, f"/v1/symbols/{SYMBOL}/special?from=2026-10-12&to=2026-10-12")
        others = await get_json(client, f"/v1/symbols/{OTHER}/special?to=2026-10-12&from=2026-10-12")

    check("special", mine)
    check("special", others)
    assert mine == {
        "symbol": SYMBOL,
        "from": "2026-10-12",
        "to": "2026-10-12",
        "special": [
            {"kind": k, "symbol": s, "start": start, "end": end, "detail": detail}
            for k, s, start, end, detail, _ in KEPT
        ],
    }
    assert starts(others) == [
        ("sidecar", None, "2026-10-12T09:21"),
        ("circuit_breaker", None, "2026-10-12T09:41"),
        ("vi", OTHER, "2026-10-12T09:50"),
        ("circuit_breaker", None, "2026-10-12T10:21"),
    ]


async def test_the_days_asked_for_and_by_default_the_last_five_trading_days(tmp_path: Path) -> None:
    engine = await two_symbols(tmp_path)  # its clock at 11:00 on Monday the 12th

    async with api(engine) as client:
        default = await get_json(client, f"/v1/symbols/{SYMBOL}/special")
        to_the_8th = await get_json(client, f"/v1/symbols/{SYMBOL}/special?to=2026-10-08")
        the_6th = await get_json(client, f"/v1/symbols/{SYMBOL}/special?from=2026-10-06&to=2026-10-06")
        sixty = await get_json(client, f"/v1/symbols/{OTHER}/special?from=2026-08-14&to=2026-10-12")

    # Friday the 9th to Tuesday the 6th: a weekday the calendar does not know is taken to trade.
    assert (default["from"], default["to"]) == ("2026-10-06", "2026-10-12")
    assert starts(default)[0] == ("sidecar", None, "2026-10-06T09:30")
    assert starts(default)[1] == ("vi", SYMBOL, "2026-10-08T10:00")
    assert len(default["special"]) == 2 + len(KEPT)
    assert (to_the_8th["from"], to_the_8th["to"]) == ("2026-10-02", "2026-10-08")
    assert starts(to_the_8th) == [("sidecar", None, "2026-10-06T09:30"), ("vi", SYMBOL, "2026-10-08T10:00")]
    assert starts(the_6th) == [("sidecar", None, "2026-10-06T09:30")]
    assert (sixty["from"], sixty["to"]) == ("2026-08-14", "2026-10-12"), "60 days, both included"
    assert starts(sixty)[:2] == [("vi", OTHER, "2026-08-14T13:00"), ("sidecar", None, "2026-10-06T09:30")]
    assert ("vi", SYMBOL, "2026-10-08T10:00") not in starts(sixty), "the symbol's VIs only"


@pytest.mark.parametrize(
    ("path", "status", "error"),
    [
        ("/v1/symbols/999999/special", 404, "unknown symbol '999999'"),
        ("/v1/symbols/005930/special?from=2026-10-1", 400, "from must be a date YYYY-MM-DD, got '2026-10-1'"),
        ("/v1/symbols/005930/special?to=2026-02-30", 400, "to must be a date YYYY-MM-DD, got '2026-02-30'"),
        (
            "/v1/symbols/005930/special?from=2026-10-13",
            400,
            "from must not be after to, got 2026-10-13 and 2026-10-12",
        ),
        (
            "/v1/symbols/005930/special?from=2026-08-13&to=2026-10-12",
            400,
            "from and to span at most 60 days, got 61",
        ),
    ],
)
async def test_an_unknown_symbol_or_a_wrong_range_is_an_error(
    tmp_path: Path, path: str, status: int, error: str
) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with api(engine) as client:
        response = await client.get(path)
        body = await response.json()

    assert (response.status, body) == (status, {"error": error})
    check("error", body)


def test_a_window_is_kept_once_by_kind_symbol_and_start(tmp_path: Path) -> None:
    store = PredictionStore(tmp_path / "engine.db")
    start = datetime(2026, 10, 12, 9, 30, 20, tzinfo=KST)
    store.keep_special(Window(Kind.VI, SYMBOL, start, start + timedelta(minutes=3)), start)
    store.keep_special(Window(Kind.VI, OTHER, start, start + timedelta(minutes=3)), start)
    store.keep_special(Window(Kind.VI, SYMBOL, start, start + timedelta(minutes=2), "static"), start)
    store.keep_special(Window(Kind.SIDECAR, None, start, start + timedelta(minutes=5)), start)
    store.keep_special(Window(Kind.SIDECAR, None, start, start + timedelta(minutes=5)), start)

    rows = store.special_windows(None, start, start + timedelta(days=1))
    store.close()

    assert [(r["kind"], r["symbol"], r["end"][11:19], r["detail"]) for r in rows] == [
        ("sidecar", None, "09:35:20", None),
        ("vi", OTHER, "09:33:20", None),
        ("vi", SYMBOL, "09:32:20", "static"),
    ]
