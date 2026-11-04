"""The fill notice recorder (#484, #403): in mock mode the trader subscribes to the real-time fill
notice of its own mock account (``kis_orders.FILL_NOTICE``) with its own key (``KRX_TRADER_KIS_*``)
and HTS ID (``KRX_TRADER_KIS_HTS_ID``), and keeps every record as it came in
``state/fill_notices.sqlite3`` (``FillNoticeStore``), for the night job ``fill-notice-layout-403`` to
measure the mock server's 23 columns against the day's orders (``notice_layout.py``). The order
probe measured every step on the mock server but this one (#403, 2026-10-30): its records had 23
fields where KIS's example lists 26 (``kis_orders.FILL_NOTICE_COLUMNS``), and the probe does not run
while the trader orders on the same mock account (#405). The trader's own orders are measured
instead.

**It never feeds the trading.** The trader knows its fills from the day's order inquiry as ever
(#404, ``kis_orders.Broker.take``); nothing here is read by a book. The recorder runs beside the
trader's loops, outside the set whose first end stops the trader (``run.py``), and ``start`` hands
it to a task that catches whatever it raises: a failure, a dropped connection or one that cannot be
made stops neither the trading nor the trader's start. It runs in mock mode alone (paper orders
nothing; the live account's notice is opened apart), and only with an HTS ID: without one it says
so once and records nothing.

**When.** Each trading day of the trader's calendar, from ``BEFORE`` (10 minutes) before KRX takes
orders (the opening auction, 08:30) to ``AFTER`` (10 minutes) after the close, 08:20 to 15:40 on a
usual day (``recording_window``): every order of the trader's is sent within it
(``mock.ORDERABLE``). Outside it there is no connection.

**The connection.** The approval key is asked through the broker's own limiter, after every call of
the trader waiting there (``kis_orders.Account.approval_key``: KIS's approval goes after the orders
and reads), kept and used again for each connection, and asked anew when KIS refuses it
(``OPSP0011``) or once it is ``APPROVAL_MAX_AGE`` old. Each connection subscribes by the HTS ID and
decrypts its frames with the key and IV of its own subscription's answer: a new connection's answer
gives new ones. PINGPONG is answered as the collector answers it. A connection that ends (KIS closed
it, an error, no frame for ``Pace.silence``, a refused subscription) is told in a WARNING and made
again after a backoff that doubles from ``Pace.backoff`` up to ``Pace.most``, from ``Pace.backoff``
again after one that was subscribed.

**What is kept.** Each data frame is decrypted once, then split by the record count its header
gives (``1|<tr_id>|<count>|<fields joined by ^>``) into records of equal width: each record's
fields as they came, empty ones too, with when it came, its trading day (KST), the TR ID, the
connection's number, the frame's number on it, the header's count and the record's place in its
frame. The columns are not named here: a record is kept as it came, not read as
``FILL_NOTICE_COLUMNS``, their first 23, or the mock server's 23 the night job's measure named
(``MOCK_FILL_NOTICE_COLUMNS``, #403). A frame whose fields do not divide by its count, or that is
not decrypted (no key and IV, or they do not open it), or not of the frame's form, is kept apart
(``odd_frames``: why, its length, and its fields when decrypted). Each connection is kept (when it
was asked for, subscribed, last heard from and ended, and why it ended), so that the night job
counts the time within the window that none was subscribed. Not kept: the approval key, the key and
IV, the subscription's answer.

**Bounds.** The frames waiting to be written (``Limits.queue``), a frame's size (``Limits.frame``,
in characters), the records and the odd frames a day (``Limits.records``, ``Limits.odd``): what is
over them is not kept, and counted (``drops``: when, the day, what bound, the frames and the records,
as many as the header said). Rows of days more than ``KEEP_DAYS`` (30) ago are deleted, at the
recorder's start and each new day.

The file holds the account, the HTS ID, order numbers and prices: it stays on the server, mode 600
with its WAL files, apart from the trader's ledger. Nothing of it goes to the Engine API, the logs,
an issue or Telegram: the night job prints shapes, counts and dates alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant import kis
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import FILL_NOTICE, decrypt
from krx_quant.market_calendar import OPENING_AUCTION, Session

log = logging.getLogger(__name__)

DB_NAME = "fill_notices.sqlite3"
MODE = 0o600
KEEP_DAYS = 30
TR_ID = FILL_NOTICE.mock  # the mock server's fill notice: the recorder runs in mock mode alone
# The recording window of a trading day (``recording_window``).
BEFORE = timedelta(minutes=10)
AFTER = timedelta(minutes=10)
# An approval key is asked anew after this long (the collector's ``_APPROVAL_MAX_AGE``).
APPROVAL_MAX_AGE = timedelta(hours=20)
# KIS's answer to a subscription whose approval key it does not take (``kis.SystemMessage``).
INVALID_APPROVAL = "OPSP0011"
# The most a WebSocket message may be before aiohttp ends the connection: a bound on memory,
# well above ``Limits.frame``.
MESSAGE_BYTES = 1 << 20
# Why a frame was kept apart (``odd_frames.why``).
UNDIVIDED = "undivided"  # its fields do not divide by its header's count (or the count is 0)
NO_KEY = "no_key"  # encrypted, and the subscription's answer gave no key and IV
UNDECRYPTED = "undecrypted"  # the key and IV did not open it
MALFORMED = "malformed"  # not ``<0|1>|<tr_id>|<count>|<fields>``
# What bound a frame dropped was over (``drops.what``).
QUEUE, SIZE, CAP = "queue", "size", "cap"
# Why a connection ended (``connections.why``), but an error's kind and text.
WINDOW_ENDED = "the recording window ended"
CLOSED_BY_KIS = "KIS closed the connection"
SILENT = "no frame for"  # ... Pace.silence seconds
REFUSED = "KIS refused the subscription"  # (KIS's code)
_CLOSED = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED)


@dataclass(frozen=True)
class Pace:
    """The recorder's waits, in seconds."""

    backoff: float = 5.0  # after a connection that ended, doubling...
    most: float = 300.0  # ...up to this
    silence: float = 180.0  # a connection with no frame (data, PINGPONG, answer) this long is dead
    heard: float = 60.0  # a connection's last frame is written at most this often
    flush: float = 3.0  # what is left to write is waited for this long when the recorder stops
    close: float = 5.0  # KIS's answer to a close is waited for this long (a silent one never answers)
    look: float = 60.0  # outside the window the clock is looked at this often


@dataclass(frozen=True)
class Limits:
    """The recorder's bounds (the module's docstring, "Bounds")."""

    queue: int = 1000  # frames waiting to be written
    frame: int = 64 * 1024  # characters a frame
    records: int = 5000  # records a day
    odd: int = 500  # odd frames a day


def recording_window(session: Session) -> tuple[datetime, datetime]:
    """When a trading day's fill notices are recorded: from ``BEFORE`` the opening auction (KRX takes
    orders from then) to ``AFTER`` the close."""
    return session.open - OPENING_AUCTION - BEFORE, session.close + AFTER


# What the recorder hands its writer, in order.


@dataclass(frozen=True)
class Frame:
    """A data frame as it came: split into ``records`` by its header's ``count``, or kept apart
    (``why``) with its ``fields`` when they were decrypted."""

    connection: int
    number: int  # the frame's number on its connection, from 1
    received_at: datetime
    tr_id: str
    count: int  # the records its header said
    length: int  # its characters as it came
    records: tuple[tuple[str, ...], ...] = ()
    why: str | None = None
    fields: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Connected:
    connection: int
    at: datetime


@dataclass(frozen=True)
class Subscribed:
    connection: int
    at: datetime


@dataclass(frozen=True)
class Heard:
    connection: int
    at: datetime


@dataclass(frozen=True)
class Ended:
    connection: int
    at: datetime
    heard: datetime | None
    why: str


Item = Frame | Connected | Subscribed | Heard | Ended
Drop = tuple[datetime, str, int, int]  # when, what bound, frames, records


def parse_frame(
    connection: int, number: int, at: datetime, text: str, key_iv: tuple[str, str] | None
) -> Frame:
    """A data frame read: decrypted once with the subscription's key and IV when it is encrypted, its
    fields split into the header's count of records of equal width, empty fields kept. Else kept
    apart, with why (``UNDIVIDED``, ``NO_KEY``, ``UNDECRYPTED``, ``MALFORMED``)."""
    parts = text.split("|", 3)
    if len(parts) != 4 or parts[0] not in ("0", "1") or not parts[2].isdigit():
        tr_id = parts[1] if len(parts) > 1 else ""
        return Frame(connection, number, at, tr_id, 0, len(text), why=MALFORMED)
    tr_id, count, payload = parts[1], int(parts[2]), parts[3]
    if parts[0] == "1":
        if key_iv is None:
            return Frame(connection, number, at, tr_id, count, len(text), why=NO_KEY)
        try:
            payload = decrypt(key_iv[0], key_iv[1], payload)
        except (ValueError, UnicodeDecodeError):
            return Frame(connection, number, at, tr_id, count, len(text), why=UNDECRYPTED)
    fields = tuple(payload.split("^"))
    if count < 1 or len(fields) % count:
        return Frame(connection, number, at, tr_id, count, len(text), why=UNDIVIDED, fields=fields)
    width = len(fields) // count
    records = tuple(fields[i * width : (i + 1) * width] for i in range(count))
    return Frame(connection, number, at, tr_id, count, len(text), records=records)


def _count_of(text: str) -> int:
    """The records a frame's header says, read without the frame (0 when it says none)."""
    parts = text.split("|", 3)
    return int(parts[2]) if len(parts) == 4 and parts[2].isdigit() else 0


# The store.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (  -- each WebSocket connection of the recorder
    id INTEGER PRIMARY KEY,               -- its number
    day TEXT NOT NULL,                    -- the KST date it was asked for on
    started_at TEXT NOT NULL,             -- when it was asked for
    subscribed_at TEXT,                   -- when KIS answered its subscription with rt_cd 0; null if never
    heard_at TEXT,                        -- its last frame of any kind (Pace.heard at most behind)
    ended_at TEXT,                        -- null while open, and when the trader was stopped hard
    why TEXT                              -- why it ended (an error's kind and text, KIS's code)
);
CREATE TABLE IF NOT EXISTS records (       -- each record of a frame split by its header's count
    id INTEGER PRIMARY KEY,
    connection INTEGER NOT NULL,
    frame INTEGER NOT NULL,               -- the frame's number on its connection
    received_at TEXT NOT NULL,
    day TEXT NOT NULL,                    -- the KST date it came on
    tr_id TEXT NOT NULL,
    count INTEGER NOT NULL,               -- the records its frame's header said
    position INTEGER NOT NULL,            -- its place in its frame, from 0
    fields TEXT NOT NULL                  -- JSON: its fields as they came, in order, empty ones too
);
CREATE INDEX IF NOT EXISTS records_by_day ON records (day);
CREATE TABLE IF NOT EXISTS odd_frames (    -- each frame not split into records
    id INTEGER PRIMARY KEY,
    connection INTEGER NOT NULL,
    frame INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    day TEXT NOT NULL,
    tr_id TEXT NOT NULL,
    count INTEGER NOT NULL,
    length INTEGER NOT NULL,              -- its characters as it came
    why TEXT NOT NULL,                    -- undivided, no_key, undecrypted, malformed
    fields TEXT                           -- JSON: its fields when decrypted; null when not
);
CREATE INDEX IF NOT EXISTS odd_frames_by_day ON odd_frames (day);
CREATE TABLE IF NOT EXISTS drops (         -- frames not kept: over a bound
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    what TEXT NOT NULL,                   -- queue, size, cap
    frames INTEGER NOT NULL,
    records INTEGER NOT NULL              -- as many as their headers said
);
"""
_TABLES = ("connections", "records", "odd_frames", "drops")


def _files(path: Path) -> tuple[Path, ...]:
    """A SQLite database and the files WAL mode keeps beside it."""
    return path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def _private(path: Path) -> None:
    """The database made, if it is not there, and it and its WAL files mode 600 (SQLite makes its
    WAL files with the database's mode)."""
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, MODE))
    for found in _files(path):
        with contextlib.suppress(FileNotFoundError):
            os.chmod(found, MODE)


class FillNoticeStore:
    """``state/fill_notices.sqlite3``, written by the recorder alone (its writer task, one call at a
    time, in worker threads), read by the night job (``read_recorded``)."""

    def __init__(self, path: Path, limits: Limits, today: date) -> None:
        self.path = path
        self._limits = limits
        _private(path)
        self._db = sqlite3.connect(path, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            _private(path)  # the WAL files are there now
            self._kept: dict[str, list[int]] = {}  # day -> [records, odd frames] kept
            self._pruned: date | None = None
            self.prune(today)
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def last_connection(self) -> int:
        """The highest connection number kept (0 for none): the next start numbers on from it."""
        row = self._db.execute("SELECT coalesce(max(id), 0) FROM connections").fetchone()
        return int(row[0])

    def prune(self, today: date) -> None:
        """Delete the rows of days more than ``KEEP_DAYS`` before ``today``, once a day."""
        if self._pruned == today:
            return
        oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()
        with self._db:
            for table in _TABLES:
                self._db.execute(f"DELETE FROM {table} WHERE day < ?", (oldest,))
        self._kept = {d: n for d, n in self._kept.items() if d >= oldest}
        self._pruned = today

    def apply(self, items: Sequence[Item], drops: Sequence[Drop] = ()) -> None:
        """Write ``items`` in order and the ``drops`` (when, what, frames, records), in one
        transaction. A frame over the day's records or odd frames is not kept: a drop of ``CAP``."""
        with self._db:
            for item in items:
                if isinstance(item, Frame):
                    if not self._keep(item, _day(item.received_at)):
                        self._drop(item.received_at, CAP, 1, item.count)
                elif isinstance(item, Connected):
                    self._db.execute(
                        "INSERT INTO connections (id, day, started_at) VALUES (?, ?, ?)",
                        (item.connection, _day(item.at), item.at.isoformat()),
                    )
                elif isinstance(item, Subscribed):
                    self._db.execute(
                        "UPDATE connections SET subscribed_at = ?, heard_at = ? WHERE id = ?",
                        (item.at.isoformat(), item.at.isoformat(), item.connection),
                    )
                elif isinstance(item, Heard):
                    self._db.execute(
                        "UPDATE connections SET heard_at = ? WHERE id = ?",
                        (item.at.isoformat(), item.connection),
                    )
                else:
                    heard = item.heard.isoformat() if item.heard is not None else None
                    self._db.execute(
                        "UPDATE connections SET ended_at = ?, why = ?, heard_at = coalesce(?, heard_at)"
                        " WHERE id = ?",
                        (item.at.isoformat(), item.why[:200], heard, item.connection),
                    )
            for at, what, frames, records in drops:
                self._drop(at, what, frames, records)

    def _keep(self, frame: Frame, day: str) -> bool:
        kept = self._kept.get(day)
        if kept is None:
            records = self._db.execute("SELECT count(*) FROM records WHERE day = ?", (day,)).fetchone()[0]
            odd = self._db.execute("SELECT count(*) FROM odd_frames WHERE day = ?", (day,)).fetchone()[0]
            kept = self._kept[day] = [int(records), int(odd)]
        at = frame.received_at.isoformat()
        if frame.why is not None:
            if kept[1] >= self._limits.odd:
                return False
            kept[1] += 1
            fields = None if frame.fields is None else json.dumps(list(frame.fields), ensure_ascii=False)
            self._db.execute(
                "INSERT INTO odd_frames (connection, frame, received_at, day, tr_id, count, length, why,"
                " fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *(frame.connection, frame.number, at, day, frame.tr_id),
                    *(frame.count, frame.length, frame.why, fields),
                ),
            )
            return True
        if kept[0] + len(frame.records) > self._limits.records:
            return False
        kept[0] += len(frame.records)
        self._db.executemany(
            "INSERT INTO records (connection, frame, received_at, day, tr_id, count, position, fields)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    frame.connection,
                    frame.number,
                    at,
                    day,
                    frame.tr_id,
                    frame.count,
                    position,
                    json.dumps(list(record), ensure_ascii=False),
                )
                for position, record in enumerate(frame.records)
            ],
        )
        return True

    def _drop(self, at: datetime, what: str, frames: int, records: int) -> None:
        self._db.execute(
            "INSERT INTO drops (at, day, what, frames, records) VALUES (?, ?, ?, ?, ?)",
            (at.isoformat(), _day(at), what, frames, records),
        )


def _day(at: datetime) -> str:
    return at.astimezone(KST).date().isoformat()


# What the night job reads (read-only).


@dataclass(frozen=True)
class StoredRecord:
    connection: int
    frame: int
    received_at: datetime
    day: date
    tr_id: str
    count: int
    position: int
    fields: tuple[str, ...]


@dataclass(frozen=True)
class StoredOdd:
    received_at: datetime
    day: date
    tr_id: str
    count: int
    why: str
    fields: tuple[str, ...] | None


@dataclass(frozen=True)
class StoredConnection:
    number: int
    day: date
    started_at: datetime
    subscribed_at: datetime | None
    heard_at: datetime | None
    ended_at: datetime | None
    why: str | None


@dataclass(frozen=True)
class StoredDrop:
    day: date
    what: str
    frames: int
    records: int


@dataclass(frozen=True)
class Recorded:
    """Everything the recorder kept."""

    records: list[StoredRecord] = field(default_factory=list)
    odd: list[StoredOdd] = field(default_factory=list)
    connections: list[StoredConnection] = field(default_factory=list)
    drops: list[StoredDrop] = field(default_factory=list)

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(sorted({r.day for r in self.records} | {o.day for o in self.odd}))


@contextlib.contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()


def read_recorded(state_dir: Path) -> Recorded | None:
    """What ``state/fill_notices.sqlite3`` holds, oldest first; None without the file. Read-only:
    ``sqlite3.Error`` when it cannot be read."""
    path = state_dir / DB_NAME
    if not path.exists():
        return None
    with _reading(path) as db:
        records = [
            StoredRecord(
                c, f, datetime.fromisoformat(at), date.fromisoformat(d), t, n, p, tuple(json.loads(x))
            )
            for c, f, at, d, t, n, p, x in db.execute(
                "SELECT connection, frame, received_at, day, tr_id, count, position, fields FROM records"
                " ORDER BY received_at, id"
            )
        ]
        odd = [
            StoredOdd(
                datetime.fromisoformat(at),
                date.fromisoformat(d),
                t,
                n,
                why,
                None if x is None else tuple(json.loads(x)),
            )
            for at, d, t, n, why, x in db.execute(
                "SELECT received_at, day, tr_id, count, why, fields FROM odd_frames ORDER BY received_at, id"
            )
        ]
        connections = [
            StoredConnection(
                i,
                date.fromisoformat(d),
                datetime.fromisoformat(s),
                _when(sub),
                _when(heard),
                _when(end),
                why,
            )
            for i, d, s, sub, heard, end, why in db.execute(
                "SELECT id, day, started_at, subscribed_at, heard_at, ended_at, why FROM connections"
                " ORDER BY id"
            )
        ]
        drops = [
            StoredDrop(date.fromisoformat(d), what, int(frames), int(n))
            for d, what, frames, n in db.execute("SELECT day, what, frames, records FROM drops ORDER BY id")
        ]
    return Recorded(records, odd, connections, drops)


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def notice_days(state_dir: Path) -> tuple[date, ...]:
    """The days the recorder kept a frame of (a record or an odd frame), oldest first: the night
    job's condition (``scheduled.Facts.fill_notice_days``). () without the file or when it cannot be
    read (a WARNING says why)."""
    path = state_dir / DB_NAME
    if not path.exists():
        return ()
    try:
        with _reading(path) as db:
            rows = db.execute(
                "SELECT day FROM records UNION SELECT day FROM odd_frames ORDER BY day"
            ).fetchall()
    except sqlite3.Error as e:
        log.warning("cannot read the recorded fill notices' days (%s)", e)
        return ()
    return tuple(date.fromisoformat(r[0]) for r in rows)


# The recorder.

Clock = Callable[[], datetime]


class FillNoticeRecorder:
    """Records the mock account's fill notices until cancelled (the module's docstring)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ws_url: str,
        hts_id: str | None,
        approval_key: Callable[[], Awaitable[str]],
        path: Path,
        clock: Clock,
        sessions: Callable[[date], Session | None],
        *,
        pace: Pace | None = None,
        limits: Limits | None = None,
    ) -> None:
        self._session = session
        self._url = ws_url.rstrip("/") + kis.WS_PATH
        self._hts_id = (hts_id or "").strip()
        self._issue = approval_key
        self._path = path
        self._clock = clock
        self._sessions = sessions
        self._pace = pace or Pace()
        self._limits = limits or Limits()
        self._approval: str | None = None
        self._approval_at: datetime | None = None
        self._queue: asyncio.Queue[Item | None] = asyncio.Queue()
        self._waiting = 0  # frames in the queue
        self._drops: list[Drop] = []  # not written yet
        self._connection = 0

    async def run(self) -> None:
        """Record until cancelled. Returns at once without an HTS ID, or when the store cannot be
        opened (a WARNING says so); nothing else ends it but its cancel."""
        if not self._hts_id:
            log.warning(
                "fill notice recorder: KRX_TRADER_KIS_HTS_ID is not set, so the mock account's fill notices"
                " are not recorded (#484); the trader trades as ever"
            )
            return
        try:
            store = await asyncio.to_thread(FillNoticeStore, self._path, self._limits, self._today())
            self._connection = await asyncio.to_thread(store.last_connection)
        except (sqlite3.Error, OSError) as e:
            log.warning(
                "fill notice recorder: cannot open %s (%s: %s), so the fill notices are not recorded; the"
                " trader trades as ever",
                self._path.name,
                type(e).__name__,
                e,
            )
            return
        log.info(
            "fill notice recorder: recording the mock account's fill notices in %s on each trading day,"
            " from %s before KRX takes orders to %s after the close (#484)",
            self._path.name,
            _span(BEFORE),
            _span(AFTER),
        )
        writer = asyncio.create_task(self._write(store))
        try:
            await self._connections()
        finally:
            self._queue.put_nowait(None)  # the writer writes what is left, then ends
            done, _ = await asyncio.wait({writer}, timeout=self._pace.flush)
            if done:
                store.close()
            else:
                writer.cancel()
                log.warning(
                    "fill notice recorder: what was left to write was not written within %gs",
                    self._pace.flush,
                )

    def _today(self) -> date:
        return self._clock().astimezone(KST).date()

    # The connections.

    async def _connections(self) -> None:
        delay = self._pace.backoff
        while True:
            window = self._window()
            if window is None:  # no trading day in the calendar's next two weeks: look again later
                await asyncio.sleep(3600.0)
                continue
            start, end = window
            if self._clock() < start:
                await asyncio.sleep(min(self._pace.look, max(0.0, (start - self._clock()).total_seconds())))
                continue
            subscribed, why = await self._connect(end)
            if why == WINDOW_ENDED:
                delay = self._pace.backoff
                continue
            if subscribed:
                delay = self._pace.backoff
            log.warning(
                "fill notice recorder: the fill notice's WebSocket ended (%s); connecting again in %gs, the"
                " trading goes on",
                why,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._pace.most)

    def _window(self) -> tuple[datetime, datetime] | None:
        """Today's recording window if it has not ended, else the next trading day's."""
        now = self._clock()
        today = now.astimezone(KST).date()
        for ahead in range(15):
            session = self._sessions(today + timedelta(days=ahead))
            if session is None:
                continue
            start, end = recording_window(session)
            if now < end:
                return start, end
        return None

    async def _connect(self, end: datetime) -> tuple[bool, str]:
        """One connection, until it ends or the window does (``end``): whether it was subscribed, and
        why it ended."""
        self._connection += 1
        number = self._connection
        self._put(Connected(number, self._clock()))
        subscribed = False
        why = CLOSED_BY_KIS
        heard: datetime | None = None
        try:
            approval = await self._approval_key()
            async with self._session.ws_connect(
                self._url,
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=self._pace.close),
                max_msg_size=MESSAGE_BYTES,
            ) as ws:
                await ws.send_str(kis.subscribe_message(approval, TR_ID, self._hts_id))
                key_iv: tuple[str, str] | None = None
                frames = 0
                written = time.monotonic()
                while True:
                    left = (end - self._clock()).total_seconds()
                    if left <= 0:
                        why = WINDOW_ENDED
                        break
                    try:
                        message = await ws.receive(timeout=min(self._pace.silence, left))
                    except TimeoutError:
                        silent = self._clock() < end
                        why = f"{SILENT} {self._pace.silence:g}s" if silent else WINDOW_ENDED
                        break
                    if message.type in _CLOSED:
                        break
                    if message.type == aiohttp.WSMsgType.ERROR:
                        why = f"{type(message.data).__name__}: {message.data}"
                        break
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    text = str(message.data)
                    heard = self._clock()
                    if time.monotonic() - written >= self._pace.heard:
                        self._put(Heard(number, heard))
                        written = time.monotonic()
                    if text[:1] in ("0", "1"):
                        frames += 1
                        self._frame(number, frames, heard, text, key_iv)
                        continue
                    answer = _system(text)
                    if answer is None:
                        continue
                    tr_id, ok, code, output = answer
                    if tr_id == "PINGPONG":
                        await ws.pong(text.encode())
                    elif tr_id == TR_ID and not subscribed:
                        if not ok:
                            if code == INVALID_APPROVAL:
                                self._approval = None  # asked anew for the next connection
                            why = f"{REFUSED} ({code or 'no code'})"
                            break
                        subscribed = True
                        key_iv = output
                        self._put(Subscribed(number, heard))
                        log.info(
                            "fill notice recorder: subscribed to the mock account's fill notice"
                            " (connection %d%s)",
                            number,
                            "" if key_iv else ", but KIS gave no key and IV: its frames are kept undecrypted",
                        )
        except (kis.KisError, aiohttp.ClientError, OSError, TimeoutError, ValueError) as e:
            why = f"{type(e).__name__}: {e}"
        finally:
            self._put(Ended(number, self._clock(), heard, why))
        return subscribed, why

    async def _approval_key(self) -> str:
        now = self._clock()
        if self._approval is None or self._approval_at is None or now - self._approval_at > APPROVAL_MAX_AGE:
            self._approval = await self._issue()
            self._approval_at = now
        return self._approval

    # The frames, and their writer.

    def _frame(
        self, connection: int, number: int, at: datetime, text: str, key_iv: tuple[str, str] | None
    ) -> None:
        if len(text) > self._limits.frame:
            self._drops.append((at, SIZE, 1, _count_of(text)))
            return
        if self._waiting >= self._limits.queue:
            self._drops.append((at, QUEUE, 1, _count_of(text)))
            return
        self._waiting += 1
        self._put(parse_frame(connection, number, at, text, key_iv))

    def _put(self, item: Item) -> None:
        self._queue.put_nowait(item)

    async def _write(self, store: FillNoticeStore) -> None:
        """Write what the recorder hands over, a batch at a time, until it hands ``None``."""
        while True:
            first = await self._queue.get()
            batch: list[Item | None] = [first]
            while not self._queue.empty() and len(batch) < 500:
                batch.append(self._queue.get_nowait())
            items = [i for i in batch if i is not None]
            frames = sum(1 for i in items if isinstance(i, Frame))
            drops, self._drops = self._drops, []
            try:
                await asyncio.to_thread(self._apply, store, items, drops)
            except Exception as e:  # the disk full, the file gone...: the recording goes on
                log.warning(
                    "fill notice recorder: could not write %d frames and %d connection changes (%s: %s);"
                    " they are lost",
                    frames,
                    len(items) - frames,
                    type(e).__name__,
                    e,
                )
            self._waiting -= frames
            if None in batch:
                return

    def _apply(self, store: FillNoticeStore, items: list[Item], drops: list[Drop]) -> None:
        store.prune(self._today())
        store.apply(items, drops)


def _system(text: str) -> tuple[str, bool, str, tuple[str, str] | None] | None:
    """A JSON control message's TR ID, whether it says success (``rt_cd`` 0), KIS's code, and the key
    and IV of a subscription's answer; None when it is not JSON of that form."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    header, body = _part(raw, "header"), _part(raw, "body")
    output = _part(body, "output")
    key, iv = output.get("key"), output.get("iv")
    return (
        str(header.get("tr_id", "")),
        str(body.get("rt_cd", "0")) == "0",
        str(body.get("msg_cd", "")),
        (str(key), str(iv)) if key and iv else None,
    )


def _part(parent: dict[str, Any], name: str) -> dict[str, Any]:
    found = parent.get(name)
    return found if isinstance(found, dict) else {}


def _span(span: timedelta) -> str:
    return f"{span.total_seconds() / 60:g} minutes"


# The trader's side (``run.py``).


def start(
    session: aiohttp.ClientSession,
    ws_url: str,
    hts_id: str | None,
    approval_key: Callable[[], Awaitable[str]],
    state_dir: Path,
    clock: Clock,
    sessions: Callable[[date], Session | None],
    *,
    pace: Pace | None = None,
    limits: Limits | None = None,
) -> asyncio.Task[None]:
    """The recorder, in a task of its own that never raises but its cancel: whatever it raises is said
    in a WARNING and ends it, the trader going on."""
    recorder = FillNoticeRecorder(
        session, ws_url, hts_id, approval_key, state_dir / DB_NAME, clock, sessions, pace=pace, limits=limits
    )
    return asyncio.create_task(_guarded(recorder), name="fill notice recorder")


async def _guarded(recorder: FillNoticeRecorder) -> None:
    try:
        await recorder.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("fill notice recorder: stopped on an error; the trader trades as ever", exc_info=True)


async def stop(task: asyncio.Task[None] | None, wait: float = 5.0) -> None:
    """Cancel the recorder and wait for it to write what it holds, ``wait`` seconds at most."""
    if task is None:
        return
    task.cancel()
    await asyncio.wait({task}, timeout=wait)
