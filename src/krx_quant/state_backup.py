"""The server state's nightly off-host backup, its restore and the monthly restore drill (#576).

Beside the Lightsail snapshots (the same account and Region, seven kept), these files lived on the
server alone: the engine's database (``engine.sqlite3``), the trader's store (``trader.sqlite3``, the
mock record the live gate reads, its mock KIS order numbers included: the user decided on 2026-11-05),
the pre-validation reports and their history (``prevalidations/``) and the scheduled jobs' state
(``scheduled.json``). The models the engine runs (``models/``) go too: a collector started on an
empty ``state/models`` publishes the defaults as ``live.json`` and prunes the store's models it no
longer names (``swap.keep_models``), so a new server needs them back before its first start. And the
experiments' reports their writers name (``EXPERIMENT_REPORTS``, #604), with those kept as records
after their experiment's code went (``KEPT_REPORTS``): neyman's holds the window's N and plan, fixed
before the window and never computed again; the user decided on 2026-11-06.

**Backup** (``back_up``). Every night at ``BACKUP_AT`` (05:35 KST: the scheduled jobs' night ends at
05:30, the 06:00 deploy comes after) the collector packs them into one ``tar.gz`` in a scratch folder
of the state folder's disk, a file at a time: each SQLite database copied by ``VACUUM INTO`` from a
read-only connection (a consistent snapshot of its last commit while the engine and the trader go on
writing; never the file as it is being written) and its rows counted by table, the other files as
they are (each is replaced whole when written, or appended to by the night's jobs, over by 05:30).
The list of files with their SHA-256 and the counts (``manifest.json``) goes in last. The pack is
uploaded to the Object Store as ``KEY``, encrypted at rest (SSE-S3, ``ObjectStore.put``'s
``encrypted``). One object, so a backup is never half one night's and half another's: an upload cut
midway leaves the last one. The last backup made is noted in ``state/state_backup.json`` (``MARK``),
which the restore drill's condition reads (``scheduled.core.Facts``). A failure is a Telegram alert,
cleared by the next backup that works (``keep_backed_up``); the next night tries again.

**Kept** 30 days: the bucket keeps every replaced version for 30 days (``deploy/create-aws.sh``), so
the object under ``KEY`` holds the last night's backup and its versions the 30 nights before. That
needs no new delete right for the engine, which can delete only ``engine/models/*``: a bug or a
leaked key cannot delete the backups. And 30 days is the drill's month: a backup the drill finds
broken still has older nights to go back to.

**Never exported**: the backup takes the files named here (``DATABASES``, ``FILES``, ``FOLDERS``,
``EXPERIMENT_REPORTS``, ``KEPT_REPORTS``) and nothing else. ``state/fill_notices.sqlite3`` (and its
``-wal``, ``-shm``) stays on the server as the docs require: ``NEVER`` names it, a backup that would
hold it is refused, a restore takes none from a pack, and no symbolic link is followed. The state
folder's other files (the KRX Open API's values in ``closing_flow/``, Toss's in ``supply/``, the KIS
token caches) are not named, so not taken. An experiment report its pattern names is still left out
when a word of its name or of a key of its JSON is an account's, an order's, a fill notice's or a
secret's (``REFUSED_WORDS``), or when it is not JSON: the manifest lists it under ``refused``, and the
drill fails on it.

**Restore** (``restore``, ``krx-quant restore-state``): the latest backup (or a pack given, such as an
older version fetched from S3) is downloaded into a scratch folder and checked as the drill checks
it, and only then moved into the state folder. What it would replace is refused, or with ``replace``
moved aside into ``state/before-restore-<time>/`` (never deleted): each database with its ``-wal``,
``-shm`` and ``-journal`` (a write-ahead log left beside a restored database would be applied to it),
each folder whole. It runs with the engine and the trader stopped: it holds the restore lock
exclusively (``state_lock``), which they hold shared from before they open a database, so a restore
beside either is refused before it changes anything, and either started meanwhile waits for it. A
rescore's engine lock refuses it too.

**Drill** (``drill``, ``krx-quant restore-drill``, the monthly scheduled job ``restore-drill-577``):
the latest backup downloaded and unpacked into a scratch folder and checked: the object's SHA-256
against the store's, each file's against the list, ``PRAGMA integrity_check`` on each database, its
tables' row counts against those counted at the backup (shown beside the live database's now), each
experiment report read as JSON (their count shown beside the live folder's now), no experiment report
refused at the backup, and the backup's age. Its output holds counts, sizes, times and ok or fail
only. True when every check passed.

The scratch folders (``state/.state-backup-work/<kind>-<random>/``) are removed when their run ends;
one a killed run left is removed by the next run once it is older than ``STALE_SCRATCH``.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from fnmatch import fnmatchcase
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import IO, Any

from krx_quant.clock import SessionClock
from krx_quant.feeds.base import KST
from krx_quant.notify import Alerts
from krx_quant.object_store import ObjectStore, file_digest
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.rescore import EngineLock
from krx_quant.state_lock import LOCK as RESTORE_LOCK
from krx_quant.state_lock import restoring
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

# The object every backup replaces (the bucket keeps the replaced ones 30 days).
KEY = "state-backup/state.tar.gz"
MANIFEST = "manifest.json"
FORMAT = 1
# What a backup holds, under the state folder: the databases, copied by VACUUM INTO; the files and
# the folders' files (every regular file at any depth, but one whose name starts with a dot: a file
# being written), copied as they are.
DATABASES = (ENGINE_DB, TRADER_DB)
FILES = ("scheduled.json",)
FOLDERS = ("prevalidations", "models")
# The experiments' reports a backup holds too (#604), files directly under ``EXPERIMENTS``: by the
# module that writes them, the pattern (``fnmatch``) of their names. Each writes one report,
# ``experiments/<its scheduled job's name>.json`` (its ``REPORT``), so each pattern is that name. A
# file no pattern names is not taken: the reports of the experiments #566 removed with their code
# (``poisson-link-439.json``, ``turing-prior-448.json``) among them, which nothing reads or writes any
# more and whose results are in their issues (#439, #448). ``tests/test_state_backup.py`` checks that
# every module naming a file under ``experiments/`` has its pattern here.
EXPERIMENTS = "experiments"
EXPERIMENT_REPORTS = {
    "krx_quant.neyman_variants": "neyman-variants-283.json",
    "krx_quant.neyman_backfill": "neyman-backfill-532.json",
}
# The reports a backup holds though their experiment's code is gone: each stays on the server as the
# record of its result, and nothing writes or reads it any more. The label-order experiment's (#529),
# removed once its result was in (none of the five a candidate, the night of 2026-11-08); the climate
# prior's (#429), whose command went with #505's other kept research commands (#658; its result made
# shannon read the prior, #435).
KEPT_REPORTS = ("normal-order-529.json", "climate-prior-429.json")
# Refused though its pattern names it (#604): an experiment report with one of these words in its
# name or in a key of its JSON, at any depth. A name or a key is split into words at anything but a
# letter or a digit and where a lower case letter meets an upper case one, then lowered; each word is
# matched alone, and each two in a row joined by "_". KIS's fields are among them: CANO and
# ACNT_PRDT_CD (the account), ODNO (the order number), CNTG_* (a fill); and the trader's (``orders``,
# ``fills``, ``broker_id``). The writers write none: one that did would keep its report on the server,
# out of the backup, and fail the drill.
REFUSED_WORDS = frozenset(
    {
        # an account's
        "account",
        "accounts",
        "acnt",
        "cano",
        # an order's
        "orders",
        "odno",
        "order_id",
        "order_no",
        "order_number",
        "broker",
        # a fill notice's
        "fill",
        "fills",
        "notice",
        "notices",
        "cntg",
        # the trader's, and the brokers' keys
        "trader",
        "kis",
        "token",
        "secret",
        "app_key",
        "app_secret",
        "password",
    }
)
# The same in Korean, found anywhere in a name or a key: account, order, fill, notice.
REFUSED_KOREAN = ("계좌", "주문", "체결", "통보")
# Never in a backup (docs/operations.md 8장): the trader's fill notice records stay on the server.
NEVER = ("fill_notices.sqlite3",)
# Each night at this time (KST), after the scheduled jobs' night (05:30) and before the 06:00 deploy.
BACKUP_AT = clock_time(5, 35)
# The last backup made on this server, under the state folder.
MARK = "state_backup.json"
# A backup older than this fails the drill: the nightly backup has not run.
STALE_AFTER = timedelta(days=2)
# The scratch folders, under the state folder; one a killed run left goes once older than this.
WORK = ".state-backup-work"
STALE_SCRATCH = timedelta(hours=2)
# Moved aside by a restore with ``replace``: ``before-restore-<time>``.
ASIDE = "before-restore-"
# The files beside an SQLite database that belong to it.
SIDE_FILES = ("-wal", "-shm", "-journal")
_LOCK = ".state-backup.lock"
_ALERT = "state-backup"
# Why an experiment report was refused, as the manifest lists it, and as the drill says it.
_BY_NAME, _BY_KEY, _NOT_JSON = "name", "key", "not json"
_REFUSED_FOR = {_BY_NAME: "이름", _BY_KEY: "JSON 키", _NOT_JSON: "JSON이 아님"}
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# gzip's fastest level: on a database of made-up predictions, a tenth more bytes than the default
# level 6 at less than half its CPU time, which the server's burstable vCPUs pay for.
_GZIP_LEVEL = 1
_MB = 1024 * 1024

if set(DATABASES + FILES + FOLDERS) & set(NEVER):
    raise RuntimeError("a backup must never hold the fill notices")


class BackupRefused(Exception):
    """A backup that would hold what must stay on the server."""


class RestoreRefused(Exception):
    """A restore beside the running engine, or one that would replace files without ``replace``."""


@dataclass(frozen=True)
class Backup:
    """A backup made: when, how many files, the object's bytes and the seconds it took; of the
    files, the experiment reports and their bytes, and the experiment reports refused."""

    made_at: datetime
    files: int
    size: int
    seconds: float
    experiments: int = 0
    experiment_bytes: int = 0
    refused: int = 0


@dataclass(frozen=True)
class Table:
    """A table's rows: counted at the backup (None: not there then), in the restored copy (None: not
    there) and, in the drill, in the live database now (None: not asked, or not there)."""

    name: str
    backed_up: int | None
    restored: int | None
    now: int | None = None

    @property
    def same(self) -> bool:
        return self.backed_up is not None and self.backed_up == self.restored


@dataclass
class Database:
    """A restored database as checked: its bytes, ``PRAGMA integrity_check``'s lines other than
    ``ok`` (0: ok; None: it could not be opened) and its tables."""

    name: str
    size: int
    problems: int | None
    tables: list[Table] = field(default_factory=list)


@dataclass
class Checked:
    """A backup downloaded, unpacked into ``root / "files"`` and checked (``check``)."""

    root: Path
    made_at: datetime | None = None
    size: int = 0  # the object's bytes
    digest: bool | None = None  # its SHA-256 against the store's; None: no store's to compare with
    files: dict[str, bool] = field(default_factory=dict)  # each listed file: its SHA-256 as listed
    databases: list[Database] = field(default_factory=list)
    experiments: dict[str, bool] = field(default_factory=dict)  # each experiment report: read as JSON
    experiment_bytes: int = 0
    refused: dict[str, str] = field(default_factory=dict)  # the experiment reports refused, and why
    live_experiments: int | None = None  # in the drill, those the live folder holds now
    problems: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)  # by step

    @property
    def ok(self) -> bool:
        return not self.problems


def next_backup(after: datetime) -> datetime:
    """The first ``BACKUP_AT`` later than ``after``."""
    local = after.astimezone(KST)
    at = datetime.combine(local.date(), BACKUP_AT, KST)
    return at if at > local else at + timedelta(days=1)


async def keep_backed_up(
    state_dir: Path, store: ObjectStore, alerts: Alerts, clock: SessionClock, *, commit: str = "unknown"
) -> None:
    """The collector's backup each night at ``BACKUP_AT``, until cancelled. A failure is alerted
    and the next night tries again; the first backup that works after it clears the alert."""
    while True:
        await clock.sleep_until(next_backup(clock.now()))
        try:
            made = await asyncio.to_thread(back_up, state_dir, store, clock.now(), commit=commit)
        except Exception as e:
            log.exception("could not back up the server state")
            alerts.raise_(
                _ALERT,
                f"서버 상태 백업(#576)이 실패했습니다({type(e).__name__}). 다음 밤 {BACKUP_AT:%H:%M}에 "
                "다시 합니다. 그동안 S3에는 그 전 밤의 백업이 남아 있습니다.",
            )
        else:
            log.info(
                "backed up the server state: %d files, %.1f MB in %.1fs (experiment reports: %d, %.1f KB)",
                made.files,
                made.size / _MB,
                made.seconds,
                made.experiments,
                made.experiment_bytes / 1024,
            )
            alerts.resolve(_ALERT, "서버 상태 백업(#576)이 다시 됩니다.")


def back_up(state_dir: Path, store: ObjectStore, now: datetime, *, commit: str = "unknown") -> Backup:
    """Pack the state's files (``DATABASES``, ``FILES``, ``FOLDERS``, ``EXPERIMENT_REPORTS`` but those
    refused) with their list and counts and upload the pack as ``KEY``, encrypted; note it in
    ``MARK``. Raises if it cannot, and stores nothing then: the store keeps the last backup."""
    began = time.monotonic()
    with _locked(state_dir), _scratch(state_dir, "backup") as work:
        files: dict[str, dict[str, Any]] = {}
        databases: dict[str, dict[str, Any]] = {}
        refused: dict[str, str] = {}
        pack = work / "state.tar.gz"
        with tarfile.open(pack, "w:gz", compresslevel=_GZIP_LEVEL) as tar:
            for name in DATABASES:
                source = state_dir / name
                if not _regular(source):
                    continue
                _refuse_never(name)
                copy = work / name
                _vacuum_into(source, copy)
                databases[name] = {"user_version": _user_version(copy), "tables": _counts(copy)}
                files[name] = _add(tar, copy, name, now)
                copy.unlink()  # one copy on the disk at a time
            for path in _plain_files(state_dir):
                name = path.relative_to(state_dir).as_posix()
                _refuse_never(name)
                copy = work / "copy"
                shutil.copyfile(path, copy)
                files[name] = _add(tar, copy, name, now)
                copy.unlink()
            for path in _experiment_files(state_dir):
                name = path.relative_to(state_dir).as_posix()
                _refuse_never(name)
                copy = work / "copy"
                shutil.copyfile(path, copy)  # checked as copied: what is packed is what was read
                if why := _refusal(name, copy):
                    log.warning("left %s out of the backup of the server state: refused (%s)", name, why)
                    refused[name] = why
                else:
                    files[name] = _add(tar, copy, name, now)
                copy.unlink()
            manifest = {
                "format": FORMAT,
                "made_at": now.astimezone(KST).isoformat(),
                "commit": commit,
                "files": files,
                "databases": databases,
                "refused": refused,
            }
            listing = work / MANIFEST
            listing.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
            _add(tar, listing, MANIFEST, now)
        size = pack.stat().st_size
        store.put(KEY, pack, file_digest(pack), encrypted=True)
    experiments = [entry for name, entry in files.items() if _experiment(name)]
    made = Backup(
        now,
        len(files),
        size,
        round(time.monotonic() - began, 1),
        experiments=len(experiments),
        experiment_bytes=sum(int(entry["size"]) for entry in experiments),
        refused=len(refused),
    )
    _write_mark(state_dir, made)
    return made


def last_backup(state_dir: Path) -> date | None:
    """The day (KST) of the last backup this server made (``MARK``); None if none, or unreadable."""
    try:
        data = json.loads((state_dir / MARK).read_text())
        return datetime.fromisoformat(str(data["made_at"])).astimezone(KST).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check(store: ObjectStore | None, root: Path, *, source: Path | None = None) -> Checked:
    """Download the latest backup from ``store`` (or take ``source``, a pack a backup made) into
    ``root``, unpack it into ``root / "files"`` and check it: the object's SHA-256 against the
    store's, each file's against the list, each database's ``PRAGMA integrity_check`` and its tables'
    row counts against those counted at the backup, each experiment report read as JSON. What fails
    is in ``problems``. The experiment reports the backup refused are in ``refused``: the drill fails
    on them, a restore does not (they are not in the backup)."""
    checked = Checked(root)
    pack = root / "state.tar.gz"
    began = time.monotonic()
    if source is not None:
        shutil.copyfile(source, pack)
    else:
        assert store is not None
        stored = store.digest(KEY)
        if stored is None:
            checked.problems.append(f"저장소에 백업({KEY})이 없다")
            return checked
        store.get(KEY, pack)
        checked.digest = file_digest(pack) == stored
        if not checked.digest:
            checked.problems.append("받은 백업의 SHA-256이 저장소의 것과 다르다")
    checked.size = pack.stat().st_size
    checked.seconds["받기"] = round(time.monotonic() - began, 1)
    began = time.monotonic()
    manifest = _unpack(pack, root / "files", checked)
    pack.unlink()  # the disk holds the unpacked files alone from here
    checked.seconds["풀기"] = round(time.monotonic() - began, 1)
    if manifest is None:
        return checked
    began = time.monotonic()
    listed: dict[str, Any] = manifest.get("databases") or {}
    for name in sorted(listed):
        checked.databases.append(_check_database(root / "files" / name, name, listed[name], checked))
    _check_experiments(root / "files", manifest, checked)
    checked.seconds["검사"] = round(time.monotonic() - began, 1)
    return checked


def drill(state_dir: Path, store: ObjectStore, now: datetime, say: Callable[[str], None] = print) -> bool:
    """The restore drill: the latest backup checked in a scratch folder, beside the live databases'
    counts and experiment reports now, its report said a line at a time; an experiment report the
    backup refused fails it. Whether every check passed."""
    with _scratch(state_dir, "drill") as work:
        checked = check(store, work)
        for database in checked.databases:
            live = _live_counts(state_dir / database.name)
            database.tables = [
                Table(t.name, t.backed_up, t.restored, live.get(t.name)) for t in database.tables
            ]
        checked.live_experiments = len(_experiment_files(state_dir))
        if refused := sorted(checked.refused.items()):
            checked.problems.append(
                f"백업이 실험 파일 {len(refused)}개를 넣지 않았다(이름이나 JSON 키에 "
                "계좌·주문·체결 통보·비밀값의 말이 있거나 JSON이 아니다): "
                + ", ".join(f"{name}({_REFUSED_FOR.get(why, why)})" for name, why in refused)
            )
        if checked.made_at is not None and now - checked.made_at > STALE_AFTER:
            checked.problems.append(
                f"가장 최근 백업이 {_age(now - checked.made_at)} 전이다: 밤 백업이 돌지 않았다"
            )
        for line in report(checked, now, title="복원 시험 (#576)", live=True):
            say(line)
    return checked.ok


def restore(
    state_dir: Path,
    store: ObjectStore | None,
    now: datetime,
    *,
    source: Path | None = None,
    replace: bool = False,
    say: Callable[[str], None] = print,
) -> bool:
    """Put the latest backup (or ``source``) back into the state folder once it checks out. Refused
    (``RestoreRefused``) before anything changes while the collector or the trader runs (they hold the
    restore lock, ``state_lock``) or a rescore does (the engine lock), and when it would replace what is
    there, unless ``replace``: that goes aside into ``before-restore-<time>/`` then. A collector or a
    trader started meanwhile waits for it to end. Whether it put the backup back; one that does not
    check out changes nothing."""
    with restoring(state_dir) as held:
        if not held:
            raise RestoreRefused(
                f"the engine or the trader is running (it holds state/{RESTORE_LOCK}): "
                "stop both first (docker compose stop engine trader)"
            )
        return _restore(state_dir, store, now, source=source, replace=replace, say=say)


def _restore(
    state_dir: Path,
    store: ObjectStore | None,
    now: datetime,
    *,
    source: Path | None,
    replace: bool,
    say: Callable[[str], None],
) -> bool:
    lock = EngineLock(state_dir)
    if not lock.acquire():
        raise RestoreRefused("a rescore is running (it holds state/.engine.lock): wait for it to end")
    try:
        with _scratch(state_dir, "restore") as work:
            checked = check(store, work, source=source)
            for line in report(checked, now, title="복원 (#576)", live=False):
                say(line)
            if not checked.ok:
                say("복원하지 않았다: 백업이 검사를 통과하지 못했다. 상태 폴더는 그대로다.")
                return False
            names = sorted(checked.files)
            there = _there(state_dir, names)
            if there and not replace:
                raise RestoreRefused(
                    "already in the state folder (--replace moves them aside): "
                    + ", ".join(p.relative_to(state_dir).as_posix() for p in there)
                )
            if there:
                aside = state_dir / f"{ASIDE}{now.astimezone(KST):%Y%m%dT%H%M%S}"
                for path in there:
                    target = aside / path.relative_to(state_dir)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(target)
                say(f"있던 것 {len(there)}개를 state/{aside.name}/로 옮겼다(지우지 않았다).")
            for name in names:
                target = state_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                (work / "files" / name).replace(target)
            say(f"복원했다: 파일 {len(names)}개를 state/에 두었다.")
            return True
    finally:
        lock.release()


def report(checked: Checked, now: datetime, *, title: str, live: bool) -> list[str]:
    """What a check found, as the drill and the restore print it: counts, sizes, times, ok or fail."""
    lines = [f"{title}: {'ok' if checked.ok else 'fail'}"]
    if checked.made_at is not None:
        lines.append(
            f"백업: {KEY}, {checked.made_at.astimezone(KST):%Y-%m-%d %H:%M} KST에 만듦"
            f"({_age(now - checked.made_at)} 전), {checked.size / _MB:,.1f} MB"
        )
    if checked.digest is not None:
        lines.append(f"받은 객체의 SHA-256: 저장소의 것과 {'같다' if checked.digest else '다르다'}")
    if checked.files:
        same = sum(checked.files.values())
        lines.append(f"파일 {len(checked.files)}개 중 SHA-256이 백업 때와 같은 파일 {same}개")
        held, read = len(checked.experiments), sum(checked.experiments.values())
        line = (
            f"실험 파일 {held}개({checked.experiment_bytes / 1024:,.1f} KB): "
            f"JSON 파싱 ok {read}개, fail {held - read}개"
        )
        if checked.live_experiments is not None:
            line += f". 지금 서버에 허용 목록의 실험 파일 {checked.live_experiments}개"
        lines.append(line)
    for database in checked.databases:
        if database.problems is None:
            integrity = "열지 못함"
        else:
            integrity = "ok" if database.problems == 0 else f"fail({database.problems}줄)"
        same = sum(t.same for t in database.tables)
        lines += [
            "",
            f"{database.name} ({database.size / _MB:,.1f} MB): integrity_check {integrity}, "
            f"표 {len(database.tables)}개 중 행 수가 백업 때와 같은 표 {same}개",
            "  표 | 백업 때 | 복원" + (" | 지금" if live else ""),
        ]
        for table in database.tables:
            row = f"  {table.name} | {_count(table.backed_up)} | {_count(table.restored)}"
            lines.append(row + (f" | {_count(table.now)}" if live else ""))
    if checked.seconds:
        lines += ["", "걸린 시간: " + ", ".join(f"{step} {s:.1f}초" for step, s in checked.seconds.items())]
    if checked.problems:
        lines += ["", "문제:", *(f"- {p}" for p in checked.problems)]
    return lines


def _age(span: timedelta) -> str:
    hours = int(span.total_seconds() // 3600)
    return f"{hours // 24}일 {hours % 24}시간"


def _count(n: int | None) -> str:
    return "없음" if n is None else f"{n:,}"


def _vacuum_into(source: Path, target: Path) -> None:
    """A consistent copy of ``source``'s last commit, read-only: its writers go on meanwhile."""
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("VACUUM INTO ?", (str(target),))
    finally:
        db.close()


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout = 30000")
        yield db
    finally:
        db.close()


def _user_version(path: Path) -> int:
    with _read_only(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _counts(path: Path) -> dict[str, int]:
    """Each table's rows, by name."""
    with _read_only(path) as db:
        names = [
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            name: int(db.execute(f"SELECT COUNT(*) FROM {_quoted(name)}").fetchone()[0]) for name in names
        }


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _live_counts(path: Path) -> dict[str, int]:
    """The live database's counts now, read-only; none if it is not there or cannot be read."""
    if not _regular(path):
        return {}
    try:
        return _counts(path)
    except sqlite3.Error:
        log.warning("could not count the rows of %s now", path.name)
        return {}


def _plain_files(state_dir: Path) -> list[Path]:
    """``FILES`` and the files under ``FOLDERS`` that are there: regular files, no symbolic link
    followed, none whose name starts with a dot."""
    found = [state_dir / name for name in FILES if _regular(state_dir / name)]
    for folder in FOLDERS:
        root = state_dir / folder
        if root.is_symlink() or not root.is_dir():
            continue
        for at, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            found += [Path(at) / n for n in sorted(names) if not n.startswith(".") and _regular(Path(at) / n)]
    return found


def _experiment_files(state_dir: Path) -> list[Path]:
    """The files directly under ``EXPERIMENTS`` that ``_named`` names, those
    a backup refuses (``_refusal``) among them: regular files, no symbolic link followed."""
    root = state_dir / EXPERIMENTS
    if root.is_symlink() or not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir()) if _named(path.name) and _regular(path)]


def _named(name: str) -> bool:
    """Whether a pattern of ``EXPERIMENT_REPORTS`` or ``KEPT_REPORTS`` names the file ``name``; never
    one whose name starts with a dot (a file being written)."""
    patterns = (*EXPERIMENT_REPORTS.values(), *KEPT_REPORTS)
    return not name.startswith(".") and any(fnmatchcase(name, p) for p in patterns)


def _experiment(name: str) -> bool:
    """Whether ``name`` (under the state folder) is an experiment report a backup may hold: directly
    under ``EXPERIMENTS``, named by a pattern, no refused word in its name."""
    parts = PurePosixPath(name).parts
    return len(parts) == 2 and parts[0] == EXPERIMENTS and _named(parts[1]) and not has_refused_word(parts[1])


def has_refused_word(text: str) -> bool:
    """Whether ``text`` (a file's name, a JSON key) has a word of ``REFUSED_WORDS`` or
    ``REFUSED_KOREAN``."""
    if any(word in text for word in REFUSED_KOREAN):
        return True
    words = [word.lower() for word in _WORD.findall(text)]
    return not REFUSED_WORDS.isdisjoint([*words, *(f"{a}_{b}" for a, b in pairwise(words))])


def _refusal(name: str, copy: Path) -> str | None:
    """Why the experiment report ``name``, copied to ``copy``, is refused: a refused word in its
    name or in a key of its JSON at any depth, or no JSON. None when it is taken."""
    if has_refused_word(PurePosixPath(name).name):
        return _BY_NAME
    try:
        data = json.loads(copy.read_bytes())
    except (ValueError, RecursionError):  # a UnicodeDecodeError is a ValueError
        return _NOT_JSON
    if any(has_refused_word(key) for key in _keys(data)):
        return _BY_KEY
    return None


def _keys(data: Any) -> Iterator[str]:
    """Every key of every object in ``data``, at any depth."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield from (str(key) for key in item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _check_experiments(files: Path, manifest: dict[str, Any], checked: Checked) -> None:
    """Each experiment report unpacked under ``files``, read as JSON; those the backup refused (its
    manifest's ``refused``)."""
    for name in sorted(checked.files):
        path = files / name
        if not _experiment(name) or not path.is_file():
            continue
        checked.experiment_bytes += path.stat().st_size
        try:
            json.loads(path.read_bytes())
        except (ValueError, RecursionError):
            checked.experiments[name] = False
        else:
            checked.experiments[name] = True
    if failed := [name for name, read in checked.experiments.items() if not read]:
        checked.problems.append(f"실험 파일 {len(failed)}개를 JSON으로 읽지 못했다")
    refused = manifest.get("refused") or {}
    if isinstance(refused, dict):
        checked.refused = {str(name): str(why) for name, why in refused.items()}
    else:
        checked.problems.append(f"백업의 목록({MANIFEST})의 refused를 읽지 못했다")


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _never(name: str) -> bool:
    return any(part.startswith(never) for part in PurePosixPath(name).parts for never in NEVER)


def _held(name: str) -> bool:
    """Whether a backup holds ``name``: a database or a file named, a file under a folder named, or
    an experiment report a pattern names (``_experiment``)."""
    parts = PurePosixPath(name).parts
    return name in DATABASES or name in FILES or (len(parts) > 1 and parts[0] in FOLDERS) or _experiment(name)


def _refuse_never(name: str) -> None:
    if _never(name):
        raise BackupRefused(f"{name} must never leave the server")


def _add(tar: tarfile.TarFile, path: Path, name: str, now: datetime) -> dict[str, Any]:
    """Add ``path`` to ``tar`` as ``name`` (owner only); its SHA-256 and size, as listed."""
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mode = 0o600
    info.mtime = int(now.timestamp())
    with path.open("rb") as f:
        tar.addfile(info, f)
    return {"sha256": file_digest(path), "size": info.size}


def _unpack(pack: Path, into: Path, checked: Checked) -> dict[str, Any] | None:
    """Unpack ``pack`` into ``into``, each file's SHA-256 against the list (``MANIFEST``); the list,
    or None if it cannot be read. Only regular files a backup holds (``_held``), never the fill
    notices, that stay under ``into`` are taken."""
    digests: dict[str, str] = {}
    manifest: Any = None
    try:
        with tarfile.open(pack, "r:gz") as tar:
            for member in tar:
                name = _safe_name(member.name)
                if name is None or not member.isfile():
                    checked.problems.append(
                        "백업에 풀지 않은 항목이 있다(일반 파일이 아니거나 경로가 밖으로 나간다)"
                    )
                    continue
                if _never(name):
                    checked.problems.append(
                        "백업에 서버 밖으로 내보내지 않는 체결 통보 기록이 있어 풀지 않았다"
                    )
                    continue
                if name != MANIFEST and not _held(name):
                    checked.problems.append("백업에 백업이 담지 않는 파일이 있어 풀지 않았다")
                    continue
                source = tar.extractfile(member)
                assert source is not None
                if name == MANIFEST:
                    manifest = json.loads(source.read())
                    continue
                target = into / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digests[name] = _write(source, target)
    except (OSError, tarfile.TarError, ValueError, EOFError) as e:
        checked.problems.append(f"백업을 풀지 못했다({type(e).__name__})")
        return None
    try:
        if manifest["format"] != FORMAT:
            raise ValueError(manifest["format"])
        checked.made_at = datetime.fromisoformat(str(manifest["made_at"]))
        listed = {str(name): str(entry["sha256"]) for name, entry in dict(manifest["files"]).items()}
    except (KeyError, ValueError, TypeError):
        checked.problems.append(f"백업에 읽을 수 있는 목록({MANIFEST}, 형식 {FORMAT})이 없다")
        return None
    checked.files = {name: digests.get(name) == digest for name, digest in sorted(listed.items())}
    if missing := set(listed) - set(digests):
        checked.problems.append(f"목록에 있는 파일 {len(missing)}개가 백업에 없다")
    if wrong := [n for n, same in checked.files.items() if not same and n in digests]:
        checked.problems.append(f"파일 {len(wrong)}개의 SHA-256이 백업 때와 다르다")
    if unlisted := set(digests) - set(listed):
        checked.problems.append(f"목록에 없는 파일 {len(unlisted)}개가 백업에 있다")
        for name in unlisted:
            (into / name).unlink(missing_ok=True)
    return dict(manifest)


def _safe_name(name: str) -> str | None:
    """``name`` as a relative path inside the unpacked folder; None if it would leave it."""
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.as_posix()


def _write(source: IO[bytes], target: Path) -> str:
    """Copy ``source`` to ``target`` (owner only), its SHA-256 on the way."""
    h = hashlib.sha256()
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _check_database(path: Path, name: str, listed: Any, checked: Checked) -> Database:
    try:
        counted = {str(t): int(n) for t, n in dict(listed["tables"]).items()}
        version = int(listed["user_version"])
    except (KeyError, ValueError, TypeError):
        checked.problems.append(f"백업의 목록에 {name}의 행 수가 없다")
        counted, version = {}, -1
    if not path.is_file():
        checked.problems.append(f"{name}이 백업에 없다")
        return Database(name, 0, None, [Table(t, n, None) for t, n in sorted(counted.items())])
    try:
        with _read_only(path) as db:
            lines = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
            restored_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        restored = _counts(path)
    except sqlite3.Error as e:
        checked.problems.append(f"{name}을 열지 못했다({type(e).__name__})")
        return Database(
            name, path.stat().st_size, None, [Table(t, n, None) for t, n in sorted(counted.items())]
        )
    problems = 0 if lines == ["ok"] else len(lines)
    if problems:
        checked.problems.append(f"{name}의 integrity_check가 문제 {problems}줄을 냈다")
    if restored_version != version:
        checked.problems.append(f"{name}의 스키마 버전(user_version)이 백업 때와 다르다")
    tables = [Table(t, counted.get(t), restored.get(t)) for t in sorted(set(counted) | set(restored))]
    if differ := [t for t in tables if not t.same]:
        checked.problems.append(f"{name}의 표 {len(differ)}개의 행 수가 백업 때와 다르다")
    return Database(name, path.stat().st_size, problems, tables)


def _there(state_dir: Path, names: Sequence[str]) -> list[Path]:
    """What putting ``names`` back would replace: of each top-level entry they are under, a
    database with its side files (a write-ahead log left beside a restored database would be applied
    to it), a file, or a folder whole (a restored folder holds what the backup held, no more)."""
    found = []
    for top in sorted({PurePosixPath(name).parts[0] for name in names}):
        path = state_dir / top
        candidates = (
            [path, *(path.with_name(path.name + s) for s in SIDE_FILES)] if top in DATABASES else [path]
        )
        found += [p for p in candidates if p.exists() or p.is_symlink()]
    return found


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """One backup at a time (the collector's night, one by hand): the second fails."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / _LOCK).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another backup of the server state is running") from None
        yield


@contextmanager
def _scratch(state_dir: Path, kind: str) -> Iterator[Path]:
    """A scratch folder on the state folder's disk, removed when done; first the ones a killed run
    left, once older than ``STALE_SCRATCH``."""
    work = state_dir / WORK
    work.mkdir(parents=True, exist_ok=True)
    for old in work.iterdir():
        try:
            if time.time() - old.stat().st_mtime > STALE_SCRATCH.total_seconds():
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue
    folder = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=work))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _write_mark(state_dir: Path, made: Backup) -> None:
    data = {
        "made_at": made.made_at.astimezone(KST).isoformat(),
        "files": made.files,
        "size": made.size,
        "seconds": made.seconds,
        "key": KEY,
    }
    tmp = state_dir / f".{MARK}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(data))
    tmp.replace(state_dir / MARK)
