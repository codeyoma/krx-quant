"""The server state's nightly backup, its restore and the monthly restore drill (#576), on a fake
object store: a local folder, and S3 by moto. Every value is made up."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import sqlite3
import tarfile
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import aiohttp
import boto3
import pytest
from moto import mock_aws

from krx_quant import state_backup, state_lock
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.notify import Alerts
from krx_quant.object_store import LocalObjectStore, ObjectStore, S3ObjectStore, file_digest
from krx_quant.process_log import Ran
from krx_quant.rescore import EngineLock
from krx_quant.scheduled import FAILED, INTERRUPTED, OK, JobRecord, Night
from krx_quant.scheduled.upkeep import RESTORE_DRILL, restore_drill_calls
from krx_quant.state_backup import (
    KEY,
    MANIFEST,
    MARK,
    BackupRefused,
    RestoreRefused,
    back_up,
    check,
    drill,
    last_backup,
    next_backup,
    restore,
)
from krx_quant.trader.run import run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from tests.collector_doubles import FIXED_NOW, settings_for
from tests.server_doubles import free_port
from tests.trader_doubles import FEE, at
from tests.waiting import until

NOW = datetime(2026, 11, 7, 5, 35, tzinfo=KST)
BUCKET = "krx-quant-test"
# The experiments' reports the state below holds, as their writers name them (#604) or as one is kept
# after its code went (#529, #429, #455, #532), made up.
EXPERIMENT_REPORTS = {
    "experiments/climate-prior-429.json": {"experiment": "climate-prior-429", "arms": {"a": {"n": 3}}},
    "experiments/normal-order-529.json": {
        "experiment": "normal-order-529",
        "comparisons": {"newton": {"ordered_over_independent": {"hit_rate": 0.01}, "label_order": True}},
    },
    "experiments/neyman-variants-283.json": {"experiment": "neyman-variants-283", "discovery": {"n": 56}},
    "experiments/neyman-backfill-532.json": {"experiment": "neyman-backfill-532", "listed": {"days": []}},
}
# What a backup of the state below holds, and what it must never hold.
HELD = {
    "engine.sqlite3",
    "trader.sqlite3",
    "scheduled.json",
    "prevalidations/kalman.json",
    "prevalidations/kalman.history.jsonl",
    "models/kalman.json",
    "models/history.json",
    "models/versions/kalman/kalman-1.json",
    *EXPERIMENT_REPORTS,
}
NEVER_HELD = (
    "fill_notices.sqlite3",
    "closing_flow/net_assets.parquet",
    "supply/symbol=005930/toss.parquet",
    "kis_token.json",
    "trader_kis_token.json",
    "market_calendar.json",
)
# Under experiments/ but named by no pattern: the reports of the experiments #566 removed, a report
# being written, a file no writer names, a report in a folder below.
NOT_NAMED = (
    "experiments/poisson-link-439.json",
    "experiments/turing-prior-448.json",
    "experiments/.neyman-variants-283.json.tmp",
    "experiments/notes.json",
    "experiments/old/climate-prior-429.json",
)


def database(path: Path, tables: dict[str, int], user_version: int = 0) -> None:
    """A database in write-ahead-log mode, as the engine and the trader keep theirs: each table with
    ``n`` made-up rows."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    for name, n in tables.items():
        db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, what TEXT, p REAL)")
        db.execute(f"CREATE INDEX {name}_what ON {name}(what)")
        db.executemany(
            f"INSERT INTO {name}(what, p) VALUES (?, ?)", ((f"x{i % 7}", i / 10) for i in range(n))
        )
    db.execute(f"PRAGMA user_version={user_version}")
    db.commit()
    db.close()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A state folder with what a backup holds, and beside it what must stay on the server."""
    root = tmp_path / "state"
    root.mkdir()
    database(root / "engine.sqlite3", {"predictions": 500, "scores": 300, "statuses": 4}, user_version=7)
    database(root / "trader.sqlite3", {"orders": 6, "trades": 3})
    database(root / "fill_notices.sqlite3", {"records": 9})
    (root / "fill_notices.sqlite3-wal").write_bytes(b"made-up wal")
    (root / "fill_notices.sqlite3-shm").write_bytes(b"made-up shm")
    (root / "scheduled.json").write_text(json.dumps({"jobs": {}, "unposted": []}))
    (root / "prevalidations").mkdir()
    (root / "prevalidations/kalman.json").write_text('{"passed": true}')
    (root / "prevalidations/kalman.history.jsonl").write_text('{"passed": true}\n')
    (root / "prevalidations/.kalman.json.1234.tmp").write_text("half written")
    (root / "prevalidations/notices.sqlite3").symlink_to(root / "fill_notices.sqlite3")
    (root / "models/versions/kalman").mkdir(parents=True)
    (root / "models/kalman.json").write_text('{"version": "kalman-1"}')
    (root / "models/history.json").write_text("[]")
    (root / "models/versions/kalman/kalman-1.json").write_text('{"version": "kalman-1"}')
    for name in NEVER_HELD[1:]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("made up")
    for name, report in EXPERIMENT_REPORTS.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(json.dumps(report, indent=2))
    for name in NOT_NAMED:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('{"made": "up"}')
    return root


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ObjectStore]:
    if request.param == "local":
        yield LocalObjectStore(tmp_path / "store")
        return
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        yield S3ObjectStore(client, BUCKET, "engine")


def members(store: ObjectStore, tmp_path: Path) -> dict[str, bytes]:
    pack = tmp_path / "fetched.tar.gz"
    store.get(KEY, pack)
    with tarfile.open(pack, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}  # type: ignore[union-attr]


def counts(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    try:
        names = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        return {n: db.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    finally:
        db.close()


def repack(store: ObjectStore, tmp_path: Path, change: dict[str, bytes | None]) -> Path:
    """The stored backup, its members changed (None: left out), packed again as a file."""
    held = members(store, tmp_path)
    for name, data in change.items():
        if data is None:
            held.pop(name, None)
        else:
            held[name] = data
    pack = tmp_path / "changed.tar.gz"
    with tarfile.open(pack, "w:gz") as tar:
        for name, data in held.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return pack


def test_a_backup_holds_the_named_files_and_never_the_fill_notices(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    made = back_up(state, store, NOW, commit="abc")

    held = members(store, tmp_path)
    assert set(held) == HELD | {MANIFEST}
    assert made.files == len(HELD)
    assert not [name for name in held if "fill_notices" in name or name.startswith(NEVER_HELD)]
    manifest = json.loads(held[MANIFEST])
    assert manifest["commit"] == "abc"
    assert set(manifest["files"]) == HELD
    assert manifest["databases"]["engine.sqlite3"] == {
        "user_version": 7,
        "tables": {"predictions": 500, "scores": 300, "statuses": 4},
    }
    assert manifest["databases"]["trader.sqlite3"]["tables"] == {"orders": 6, "trades": 3}
    assert held["prevalidations/kalman.json"] == b'{"passed": true}'
    assert last_backup(state) == NOW.date()
    assert json.loads((state / MARK).read_text())["size"] == made.size
    # the scratch folder went with the run
    assert list((state / state_backup.WORK).iterdir()) == []


def test_a_backup_that_would_hold_the_fill_notices_is_refused_and_stores_nothing(
    state: Path, store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_backup, "FILES", ("scheduled.json", "fill_notices.sqlite3"))

    with pytest.raises(BackupRefused):
        back_up(state, store, NOW)

    assert store.digest(KEY) is None
    assert last_backup(state) is None


def test_a_backup_holds_the_experiment_reports_their_writers_name_and_no_other_file_there(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    made = back_up(state, store, NOW)

    held = members(store, tmp_path)
    assert {name for name in held if name.startswith("experiments/")} == set(EXPERIMENT_REPORTS)
    for name, report in EXPERIMENT_REPORTS.items():
        assert json.loads(held[name]) == report
    assert not set(NOT_NAMED) & set(held)
    assert json.loads(held[MANIFEST])["refused"] == {}
    assert made.experiments == len(EXPERIMENT_REPORTS)
    assert made.experiment_bytes == sum((state / name).stat().st_size for name in EXPERIMENT_REPORTS)


def test_an_experiment_report_that_is_a_symbolic_link_is_not_followed(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    (state / "experiments/climate-prior-429.json").unlink()
    (state / "experiments/climate-prior-429.json").symlink_to(state / "trader.sqlite3")

    back_up(state, store, NOW)

    assert "experiments/climate-prior-429.json" not in members(store, tmp_path)


def writers() -> set[str]:
    """The modules whose code names a file under ``experiments/``."""
    src = Path(state_backup.__file__).parent
    return {
        ".".join(("krx_quant", *path.relative_to(src).with_suffix("").parts))
        for path in src.rglob("*.py")
        if re.search(r"""["']experiments/""", path.read_text())
    }


def test_each_module_that_writes_under_experiments_has_the_pattern_of_its_report() -> None:
    assert writers() == set(state_backup.EXPERIMENT_REPORTS)
    for module, pattern in state_backup.EXPERIMENT_REPORTS.items():
        report = PurePosixPath(importlib.import_module(module).REPORT)
        assert report.parent.as_posix() == state_backup.EXPERIMENTS
        assert fnmatchcase(report.name, pattern), module
        assert not state_backup.has_refused_word(pattern), pattern


def test_a_kept_report_has_no_writer_left_and_no_refused_word() -> None:
    """The reports kept after their experiment's code went (#529's, #429's, #455's and #532's) are named
    by no module's pattern and no module writes them; the backup still takes them
    (``EXPERIMENT_REPORTS`` above)."""
    assert state_backup.KEPT_REPORTS == (
        "normal-order-529.json",
        "climate-prior-429.json",
        "neyman-variants-283.json",
        "neyman-backfill-532.json",
    )
    for gone in ("normal_order", "climate_prior", "neyman_variants", "neyman_backfill"):
        assert importlib.util.find_spec(f"krx_quant.{gone}") is None, gone
    for name in state_backup.KEPT_REPORTS:
        assert not any(fnmatchcase(name, p) for p in state_backup.EXPERIMENT_REPORTS.values()), name
        assert not state_backup.has_refused_word(name), name


@pytest.fixture
def any_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pattern that names any JSON file under experiments/: the refusals stand without one."""
    monkeypatch.setattr(
        state_backup, "EXPERIMENT_REPORTS", {**state_backup.EXPERIMENT_REPORTS, "tests.any": "*.json"}
    )


@pytest.mark.usefixtures("any_report")
@pytest.mark.parametrize(
    "name",
    [
        "kis-account-balance.json",
        "accounts.json",
        "trader-orders.json",
        "fill_notices.json",
        "fillNotice.json",
        "mock-fills-403.json",
        "cntg-probe.json",
        "trader_kis_token.json",
        "주문-기록.json",
    ],
)
def test_a_report_with_a_word_of_an_account_order_or_fill_notice_in_its_name_is_refused(
    state: Path, store: ObjectStore, tmp_path: Path, name: str
) -> None:
    (state / "experiments" / name).write_text('{"n": 3}')

    made = back_up(state, store, NOW)

    held = members(store, tmp_path)
    assert f"experiments/{name}" not in held
    assert json.loads(held[MANIFEST])["refused"] == {f"experiments/{name}": "name"}
    assert "experiments/notes.json" in held  # what the pattern names besides is taken
    assert made.refused == 1


@pytest.mark.usefixtures("any_report")
def test_a_pack_holding_an_experiment_report_with_a_refused_name_is_not_unpacked(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    pack = repack(store, tmp_path, {"experiments/trader-orders.json": b"{}"})
    root = tmp_path / "checked"
    root.mkdir()

    checked = check(None, root, source=pack)

    assert "백업에 백업이 담지 않는 파일이 있어 풀지 않았다" in checked.problems
    assert not list(root.rglob("trader-orders*"))


@pytest.mark.parametrize(
    "key",
    ["CANO", "ACNT_PRDT_CD", "account", "accountNo", "ODNO", "order_id", "orderNo", "broker_id"]
    + ["fills", "fill_notice", "CNTG_QTY", "token", "app_secret", "체결"],
)
def test_a_report_with_a_key_of_an_account_order_or_fill_notice_is_refused(
    state: Path, store: ObjectStore, tmp_path: Path, caplog: pytest.LogCaptureFixture, key: str
) -> None:
    """Even under the name its writer gives it, and however deep the key."""
    caplog.set_level(logging.WARNING, "krx_quant.state_backup")
    report = {"experiment": "neyman-variants-283", "evaluation": {"by_day": [{"day": "2026-11-02"}]}}
    report["evaluation"]["by_day"][0][key] = "made up"  # type: ignore[index]
    (state / "experiments/neyman-variants-283.json").write_text(json.dumps(report))

    back_up(state, store, NOW)

    held = members(store, tmp_path)
    assert "experiments/neyman-variants-283.json" not in held
    assert json.loads(held[MANIFEST])["refused"] == {"experiments/neyman-variants-283.json": "key"}
    assert (
        "left experiments/neyman-variants-283.json out of the backup of the server state: refused (key)"
        in caplog.messages
    )


def test_an_experiment_report_that_is_not_json_is_refused(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    (state / "experiments/normal-order-529.json").write_text('{"experiment": "normal-order-529", ')

    back_up(state, store, NOW)

    held = members(store, tmp_path)
    assert "experiments/normal-order-529.json" not in held
    assert json.loads(held[MANIFEST])["refused"] == {"experiments/normal-order-529.json": "not json"}


@pytest.mark.parametrize(
    "words",
    ["normal-order-529.json", "label_order", "ordered_over_independent", "filled_days", "notes", "keys"],
)
def test_the_words_of_the_experiments_as_they_write_are_not_refused(words: str) -> None:
    assert not state_backup.has_refused_word(words)


def test_the_database_copy_is_its_last_commit_while_a_writer_goes_on(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    writer = sqlite3.connect(state / "engine.sqlite3")
    writer.execute("INSERT INTO predictions(what, p) VALUES ('later', 0.5)")  # not committed yet
    try:
        back_up(state, store, NOW)
    finally:
        writer.commit()
        writer.close()

    manifest = json.loads(members(store, tmp_path)[MANIFEST])
    assert manifest["databases"]["engine.sqlite3"]["tables"]["predictions"] == 500
    assert counts(state / "engine.sqlite3")["predictions"] == 501


def test_on_s3_the_backup_is_encrypted_at_rest(state: Path) -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        back_up(state, S3ObjectStore(client, BUCKET, "engine"), NOW)

        head = client.head_object(Bucket=BUCKET, Key=f"engine/{KEY}")
    assert head["ServerSideEncryption"] == "AES256"
    assert head["Metadata"]["sha256"]


def test_the_drill_passes_on_the_latest_backup_and_leaves_the_state_as_it_is(
    state: Path, store: ObjectStore
) -> None:
    back_up(state, store, NOW)
    before = sorted(p.relative_to(state).as_posix() for p in state.rglob("*"))
    lines: list[str] = []

    assert drill(state, store, NOW + timedelta(hours=17), lines.append)

    assert lines[0] == "복원 시험 (#576): ok"
    assert f"파일 {len(HELD)}개 중 SHA-256이 백업 때와 같은 파일 {len(HELD)}개" in lines
    assert any(
        line.startswith("engine.sqlite3 (")
        and "integrity_check ok, 표 3개 중 행 수가 백업 때와 같은 표 3개" in line
        for line in lines
    )
    assert "  predictions | 500 | 500 | 500" in lines
    assert "  orders | 6 | 6 | 6" in lines
    assert not [line for line in lines if line.startswith("문제")]
    assert sorted(p.relative_to(state).as_posix() for p in state.rglob("*")) == before


def experiments_line(held: int, size: int, ok: int, now: int | None = None) -> str:
    line = f"실험 파일 {held}개({size / 1024:,.1f} KB): JSON 파싱 ok {ok}개, fail {held - ok}개"
    return line if now is None else f"{line}. 지금 서버에 허용 목록의 실험 파일 {now}개"


def test_the_drill_counts_the_experiment_reports_and_reads_each_as_json(
    state: Path, store: ObjectStore
) -> None:
    back_up(state, store, NOW)
    size = sum((state / name).stat().st_size for name in EXPERIMENT_REPORTS)
    (state / "experiments/neyman-backfill-532.json").unlink()  # since the backup
    lines: list[str] = []

    assert drill(state, store, NOW + timedelta(hours=17), lines.append)

    assert experiments_line(4, size, 4, now=3) in lines


def test_a_backup_whose_experiment_report_does_not_read_as_json_fails_the_check(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    broken = b'{"experiment": "climate-prior-429", '
    manifest = json.loads(members(store, tmp_path)[MANIFEST])
    manifest["files"]["experiments/climate-prior-429.json"] = {
        "sha256": hashlib.sha256(broken).hexdigest(),
        "size": len(broken),
    }
    pack = repack(
        store,
        tmp_path,
        {"experiments/climate-prior-429.json": broken, MANIFEST: json.dumps(manifest).encode()},
    )
    (tmp_path / "checked").mkdir()

    checked = check(None, tmp_path / "checked", source=pack)

    assert checked.problems == ["실험 파일 1개를 JSON으로 읽지 못했다"]  # its SHA-256 is as listed
    assert checked.experiments["experiments/climate-prior-429.json"] is False
    assert sum(checked.experiments.values()) == 3


def test_the_drill_fails_when_the_backup_refused_an_experiment_report(
    state: Path, store: ObjectStore
) -> None:
    (state / "experiments/neyman-variants-283.json").write_text('{"discovery": {"CANO": "made up"}}')
    back_up(state, store, NOW)
    lines: list[str] = []

    assert not drill(state, store, NOW + timedelta(hours=17), lines.append)

    assert lines[0] == "복원 시험 (#576): fail"
    assert (
        "- 백업이 실험 파일 1개를 넣지 않았다(이름이나 JSON 키에 계좌·주문·체결 통보·비밀값의 말이 있거나 "
        "JSON이 아니다): experiments/neyman-variants-283.json(JSON 키)"
    ) in lines


def test_the_drill_shows_the_live_counts_now_beside_those_backed_up(state: Path, store: ObjectStore) -> None:
    back_up(state, store, NOW)
    db = sqlite3.connect(state / "engine.sqlite3")
    db.execute("INSERT INTO predictions(what, p) VALUES ('later', 0.5)")
    db.commit()
    db.close()
    lines: list[str] = []

    assert drill(state, store, NOW + timedelta(hours=17), lines.append)

    assert "  predictions | 500 | 500 | 501" in lines


def test_the_drill_fails_without_a_backup(state: Path, store: ObjectStore) -> None:
    lines: list[str] = []

    assert not drill(state, store, NOW, lines.append)

    assert lines[0] == "복원 시험 (#576): fail"
    assert f"- 저장소에 백업({KEY})이 없다" in lines


def test_the_drill_fails_on_a_backup_two_days_old(state: Path, store: ObjectStore) -> None:
    back_up(state, store, NOW)
    lines: list[str] = []

    assert not drill(state, store, NOW + timedelta(days=2, hours=1), lines.append)

    assert "- 가장 최근 백업이 2일 1시간 전이다: 밤 백업이 돌지 않았다" in lines


def test_a_backup_whose_database_was_damaged_fails_the_check(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    damaged = bytearray(members(store, tmp_path)["engine.sqlite3"])
    damaged[4096 : 4096 + 12] = b"\xff" * 12  # the second page's b-tree header
    pack = repack(store, tmp_path, {"engine.sqlite3": bytes(damaged)})
    (tmp_path / "checked").mkdir()

    checked = check(None, tmp_path / "checked", source=pack)

    assert not checked.ok
    assert "파일 1개의 SHA-256이 백업 때와 다르다" in checked.problems
    engine = next(d for d in checked.databases if d.name == "engine.sqlite3")
    assert engine.problems != 0


def test_a_backup_whose_counts_differ_from_its_list_fails_the_check(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    manifest = json.loads(members(store, tmp_path)[MANIFEST])
    manifest["databases"]["trader.sqlite3"]["tables"]["orders"] = 7
    pack = repack(store, tmp_path, {MANIFEST: json.dumps(manifest).encode()})
    (tmp_path / "checked").mkdir()

    checked = check(None, tmp_path / "checked", source=pack)

    assert "trader.sqlite3의 표 1개의 행 수가 백업 때와 다르다" in checked.problems
    trader = next(d for d in checked.databases if d.name == "trader.sqlite3")
    assert [(t.name, t.backed_up, t.restored) for t in trader.tables if not t.same] == [("orders", 7, 6)]


def test_a_backup_missing_a_listed_file_fails_the_check(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    pack = repack(store, tmp_path, {"models/kalman.json": None})
    (tmp_path / "checked").mkdir()

    checked = check(None, tmp_path / "checked", source=pack)

    assert "목록에 있는 파일 1개가 백업에 없다" in checked.problems
    assert checked.files["models/kalman.json"] is False


def test_a_pack_reaching_out_of_its_folder_or_holding_the_fill_notices_is_not_unpacked(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    pack = repack(
        store,
        tmp_path,
        {
            "../outside.json": b"{}",
            "fill_notices.sqlite3": b"made up",
            "kis_token.json": b"{}",
            "experiments/kis-orders.json": b"{}",
            "experiments/poisson-link-439.json": b"{}",
        },
    )
    root = tmp_path / "checked"
    root.mkdir()

    checked = check(None, root, source=pack)

    assert not checked.ok
    assert not (tmp_path / "outside.json").exists()
    assert not list(root.rglob("fill_notices*"))
    assert not list(root.rglob("kis_token*"))
    assert not list(root.rglob("kis-orders*"))
    assert not list(root.rglob("poisson-link*"))
    assert "백업에 서버 밖으로 내보내지 않는 체결 통보 기록이 있어 풀지 않았다" in checked.problems
    assert "백업에 백업이 담지 않는 파일이 있어 풀지 않았다" in checked.problems


def test_on_s3_a_backup_unlike_the_stored_digest_fails_the_drill(state: Path) -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        store = S3ObjectStore(client, BUCKET, "engine")
        back_up(state, store, NOW)
        head = client.head_object(Bucket=BUCKET, Key=f"engine/{KEY}")
        client.put_object(Bucket=BUCKET, Key=f"engine/{KEY}", Body=b"not a backup", Metadata=head["Metadata"])
        lines: list[str] = []

        assert not drill(state, store, NOW, lines.append)

    assert "받은 객체의 SHA-256: 저장소의 것과 다르다" in lines


def test_a_restore_puts_the_backup_back_into_a_new_servers_state_folder(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    new = tmp_path / "new-state"
    new.mkdir()
    lines: list[str] = []

    assert restore(new, store, NOW + timedelta(hours=1), say=lines.append)

    assert lines[0] == "복원 (#576): ok"
    assert lines[-1] == f"복원했다: 파일 {len(HELD)}개를 state/에 두었다."
    assert {p.relative_to(new).as_posix() for p in new.rglob("*") if p.is_file()} >= HELD
    assert counts(new / "engine.sqlite3") == counts(state / "engine.sqlite3")
    assert counts(new / "trader.sqlite3") == {"orders": 6, "trades": 3}
    db = sqlite3.connect(new / "engine.sqlite3")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    db.close()
    assert (new / "models/versions/kalman/kalman-1.json").read_text() == '{"version": "kalman-1"}'
    neyman = "experiments/neyman-variants-283.json"
    assert json.loads((new / neyman).read_text()) == EXPERIMENT_REPORTS[neyman]
    assert not [name for name in NOT_NAMED if (new / name).exists()]
    assert not list(new.rglob("fill_notices*"))
    assert ((new / "trader.sqlite3").stat().st_mode & 0o777) == 0o600


def test_a_restore_over_files_there_is_refused_unless_it_moves_them_aside(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    other = tmp_path / "other-state"
    other.mkdir()
    database(other / "engine.sqlite3", {"predictions": 2})
    (other / "engine.sqlite3-wal").write_bytes(b"a log of another database")
    (other / "models").mkdir()
    (other / "models/turing.json").write_text("{}")
    (other / "experiments").mkdir()
    (other / "experiments/poisson-link-439.json").write_text("{}")
    (other / "fill_notices.sqlite3").write_bytes(b"kept as it is")

    before = file_digest(other / "engine.sqlite3")

    with pytest.raises(RestoreRefused, match="engine.sqlite3, engine.sqlite3-wal, experiments, models"):
        restore(other, store, NOW, say=lambda line: None)
    assert file_digest(other / "engine.sqlite3") == before
    assert (other / "engine.sqlite3-wal").exists()

    assert restore(other, store, NOW, replace=True, say=lambda line: None)

    aside = other / "before-restore-20261107T053500"
    assert (aside / "engine.sqlite3-wal").read_bytes() == b"a log of another database"
    assert file_digest(aside / "engine.sqlite3") == before
    assert (aside / "models/turing.json").exists()
    assert (aside / "experiments/poisson-link-439.json").exists()  # the backup does not hold it
    assert (other / "experiments/neyman-variants-283.json").exists()
    assert not (other / "engine.sqlite3-wal").exists()
    assert not (other / "models/turing.json").exists()  # the restored folder holds what was backed up
    assert counts(other / "engine.sqlite3")["predictions"] == 500
    assert (other / "fill_notices.sqlite3").read_bytes() == b"kept as it is"


def test_a_restore_beside_a_rescore_is_refused(state: Path, store: ObjectStore, tmp_path: Path) -> None:
    back_up(state, store, NOW)
    new = tmp_path / "new-state"
    engine = EngineLock(new)
    assert engine.acquire()
    try:
        with pytest.raises(RestoreRefused, match="a rescore is running"):
            restore(new, store, NOW, say=lambda line: None)
    finally:
        engine.release()
    assert not (new / "engine.sqlite3").exists()


def test_a_backup_that_does_not_check_out_restores_nothing(
    state: Path, store: ObjectStore, tmp_path: Path
) -> None:
    back_up(state, store, NOW)
    pack = repack(store, tmp_path, {"scheduled.json": b"changed"})
    new = tmp_path / "new-state"
    new.mkdir()
    lines: list[str] = []

    assert not restore(new, None, NOW, source=pack, say=lines.append)

    assert lines[-1] == "복원하지 않았다: 백업이 검사를 통과하지 못했다. 상태 폴더는 그대로다."
    assert sorted(p.name for p in new.iterdir()) == [".engine.lock", state_backup.WORK, state_lock.LOCK]
    assert list((new / state_backup.WORK).iterdir()) == []


def test_a_scratch_folder_a_killed_run_left_goes_with_the_next_run(
    state: Path, store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = state / state_backup.WORK / "drill-killed"
    left.mkdir(parents=True)
    (left / "engine.sqlite3").write_bytes(b"made up")
    young = state / state_backup.WORK / "drill-running"
    young.mkdir()
    old = (datetime.now(KST) - timedelta(hours=3)).timestamp()
    os.utime(left, (old, old))

    back_up(state, store, NOW)

    assert not left.exists()
    assert young.exists()


class Failing(LocalObjectStore):
    """A store that refuses uploads while ``fail``."""

    fail = True

    def put(self, key: str, source: Path, digest: str, *, encrypted: bool = False) -> None:
        if self.fail:
            raise OSError("store unreachable")
        super().put(key, source, digest, encrypted=encrypted)


async def test_the_backup_runs_each_night_at_05_35_and_a_failure_is_alerted(
    state: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "krx_quant.notify")
    store = Failing(tmp_path / "store")
    clock = FakeClock(datetime(2026, 11, 6, 23, 0, tzinfo=KST))

    def alerted(text: str) -> bool:
        return any(text in message for message in caplog.messages)

    async with aiohttp.ClientSession() as session:
        alerts = Alerts(session, None, None, "http://127.0.0.1:9")
        task = asyncio.create_task(state_backup.keep_backed_up(state, store, alerts, clock, commit="c"))
        try:
            await until(lambda: bool(clock._waiters))
            clock.set(datetime(2026, 11, 7, 5, 34, tzinfo=KST))
            await until(lambda: bool(clock._waiters))  # it looked, and waits on
            assert store.digest(KEY) is None
            clock.set(datetime(2026, 11, 7, 5, 35, tzinfo=KST))
            await until(lambda: alerted("[장애] 서버 상태 백업(#576)이 실패했습니다(OSError)"))
            assert last_backup(state) is None
            store.fail = False
            await until(lambda: bool(clock._waiters))
            clock.set(datetime(2026, 11, 8, 5, 35, tzinfo=KST))
            await until(lambda: alerted("[복구] 서버 상태 백업(#576)이 다시 됩니다."))
        finally:
            task.cancel()
            await alerts.close()

    assert store.digest(KEY) is not None
    assert last_backup(state) == date(2026, 11, 8)


@pytest.mark.parametrize(
    ("now", "then"),
    [
        (datetime(2026, 11, 7, 5, 34, tzinfo=KST), datetime(2026, 11, 7, 5, 35, tzinfo=KST)),
        (datetime(2026, 11, 7, 5, 35, tzinfo=KST), datetime(2026, 11, 8, 5, 35, tzinfo=KST)),
        (datetime(2026, 11, 6, 23, 0, tzinfo=KST), datetime(2026, 11, 7, 5, 35, tzinfo=KST)),
    ],
)
def test_the_next_backup_is_the_next_05_35(now: datetime, then: datetime) -> None:
    assert next_backup(now) == then


def night(
    day: date, record: JobRecord | None, backed_up: date | None = date(2026, 11, 7)
) -> Night[date | None]:
    return Night(day, True, "c", record, {}, backed_up)


def ran(day: date, outcome: str) -> JobRecord:
    return JobRecord(day, outcome, datetime.combine(day, time(22, 30), KST), "c")


def test_the_drill_waits_for_the_servers_first_backup() -> None:
    assert not RESTORE_DRILL.due(night(date(2026, 11, 7), None, backed_up=None))
    assert RESTORE_DRILL.due(night(date(2026, 11, 8), None))


def test_the_drill_runs_on_the_first_night_of_each_month() -> None:
    drilled = ran(date(2026, 11, 8), OK)

    assert not RESTORE_DRILL.due(night(date(2026, 11, 9), drilled))
    assert not RESTORE_DRILL.due(night(date(2026, 11, 30), drilled))
    assert RESTORE_DRILL.due(night(date(2026, 12, 1), drilled))
    assert RESTORE_DRILL.due(night(date(2027, 1, 1), ran(date(2026, 12, 1), OK)))


def test_a_failed_drill_runs_again_seven_nights_later_and_a_cut_one_the_next_night() -> None:
    failed = ran(date(2026, 12, 1), FAILED)

    assert not RESTORE_DRILL.due(night(date(2026, 12, 7), failed))
    assert RESTORE_DRILL.due(night(date(2026, 12, 8), failed))
    assert RESTORE_DRILL.due(night(date(2026, 12, 2), ran(date(2026, 12, 1), INTERRUPTED)))


def test_only_a_drill_that_failed_calls_an_agent() -> None:
    assert RESTORE_DRILL.targets[0].issue == 577
    assert RESTORE_DRILL.command == ("restore-drill",)
    assert RESTORE_DRILL.unlabel == ()
    assert not restore_drill_calls(Ran(0, "복원 시험 (#576): ok\n", ""))
    assert restore_drill_calls(Ran(1, "복원 시험 (#576): fail\n", ""))
    assert restore_drill_calls(Ran(None, "", "", timed_out=True))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_OBJECT_STORE", str(tmp_path / "store"))
    return tmp_path


def test_a_new_servers_setup_restores_nothing_without_a_backup(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["restore-state", "--if-backed-up"]) == 0

    assert "no backup of the server state in the store" in capsys.readouterr().out
    assert not (env / "state/engine.sqlite3").exists()


def test_the_commands_back_up_drill_and_restore(
    state: Path, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backup-state"]) == 0
    assert main(["restore-drill"]) == 0
    assert "복원 시험 (#576): ok" in capsys.readouterr().out
    (state / "scheduled.json").unlink()

    assert main(["restore-state"]) == 1  # the databases are there
    assert main(["restore-state", "--if-backed-up", "--replace"]) == 0

    assert json.loads((state / "scheduled.json").read_text()) == {"jobs": {}, "unposted": []}
    assert file_digest(state / "fill_notices.sqlite3")  # left where it was


@pytest.mark.parametrize("who", ["the collector", "the trader"])
async def test_a_restore_beside_the_running_collector_or_trader_is_refused_and_changes_nothing(
    state: Path, store: ObjectStore, tmp_path: Path, who: str
) -> None:
    """Codex on #581: a restore with --replace must not swap the databases under a running engine."""
    back_up(state, store, NOW)
    other = tmp_path / "other-state"
    other.mkdir()
    database(other / "engine.sqlite3", {"predictions": 2})
    before = file_digest(other / "engine.sqlite3")

    async with state_lock.in_use(other, who):
        with pytest.raises(RestoreRefused, match="the engine or the trader is running"):
            restore(other, store, NOW, replace=True, say=lambda line: None)

    assert file_digest(other / "engine.sqlite3") == before
    assert not [p for p in other.iterdir() if p.name.startswith(state_backup.ASIDE)]
    assert restore(other, store, NOW, replace=True, say=lambda line: None)  # once it has stopped


async def test_a_collector_started_during_a_restore_waits_for_it_before_opening_its_database(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, "krx_quant.state_lock")
    monkeypatch.setattr(state_lock, "POLL_SECONDS", 0.02)
    settings = settings_for(kis, tmp_path / "data")
    stop = asyncio.Event()
    with state_lock.restoring(settings.state_dir) as held:
        assert held
        task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
        try:
            await until(
                lambda: (
                    "a restore of the server state runs: the collector waits for it to end" in caplog.messages
                )
            )
            assert not (settings.state_dir / "engine.sqlite3").exists()
        except BaseException:
            stop.set()
            raise
    try:
        await until(lambda: (settings.state_dir / "engine.sqlite3").exists())
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert "the restore of the server state is over: the collector starts" in caplog.messages


async def test_a_trader_started_during_a_restore_waits_for_it_before_opening_its_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, "krx_quant.state_lock")
    monkeypatch.setattr(state_lock, "POLL_SECONDS", 0.02)
    state = tmp_path / "state"
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=state, fee_rate=FEE)
    trader = TraderSettings(
        quotes_url=f"ws://127.0.0.1:{free_port()}/v1/quotes",
        engine_url=f"http://127.0.0.1:{free_port()}",
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    with state_lock.restoring(state) as held:
        assert held
        task = asyncio.create_task(run_trader(settings, trader, stop=stop, now=lambda: at("10:00:00")))
        try:
            await until(
                lambda: (
                    "a restore of the server state runs: the trader waits for it to end" in caplog.messages
                )
            )
            assert not (state / TRADER_DB).exists()
        except BaseException:
            stop.set()
            raise
    try:
        await until(lambda: (state / TRADER_DB).exists())
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
