"""The memory the engine's heartbeat says each minute (#547): the server's available memory and swap,
the engine's resident memory, the trader's from its snapshot, each running training or job's.

Input: a made-up ``/proc`` (``meminfo``, ``self/status``, ``<pid>/status``, in the kernel's layout)
and a trader store (``state/trader.sqlite3``) with a snapshot of a given age. Output: the line's
memory part, and the whole line as Loki takes it. The patterns the docs' LogQL uses
(docs/operations.md, docs/agents/server-logs.md) are checked against it here.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from krx_quant import heartbeat, loki, process_log
from krx_quant.feeds.base import KST
from krx_quant.process_log import formatter
from krx_quant.trader import paper
from krx_quant.trader.store import DB_NAME, TRADER_FIELDS, TraderStore, read_resident, read_trader
from tests.loki_fake import FakeLoki
from tests.trader_doubles import Day, Tick, at
from tests.waiting import until

NOW = datetime(2026, 11, 5, 10, 0, 0, tzinfo=KST)
JOB_PID, TRAINING_PID = 4242, 4243


def fake_proc(root: Path) -> Path:
    """``/proc`` with the server's memory, this process's and two children's (in kB, as the kernel
    writes them): 812 MB available, 20 MB of swap in use; the engine 431 MB, a job 300 MB, the
    training 640 MB."""
    root.mkdir()
    (root / "meminfo").write_text(
        "MemTotal:        1969600 kB\n"
        "MemFree:          120000 kB\n"
        "MemAvailable:     831488 kB\n"
        "SwapTotal:       2097148 kB\n"
        "SwapFree:        2076668 kB\n"
    )
    for name, kb in (("self", 441_344), (str(JOB_PID), 307_200), (str(TRAINING_PID), 655_360)):
        (root / name).mkdir()
        (root / name / "status").write_text(f"Name:\tpython\nVmHWM:\t{kb + 1024} kB\nVmRSS:\t{kb} kB\n")
    return root


def snapshot(state: Path, written: datetime, body: dict[str, object]) -> None:
    store = TraderStore(state / DB_NAME)
    try:
        store.put_snapshot(written, body)
    finally:
        store.close()


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job and the training running, as ``run_logged`` names them, the job started first."""
    monkeypatch.setattr(process_log, "_running", {JOB_PID: "job prevalidate-cox", TRAINING_PID: "training"})


async def test_the_heartbeat_says_the_server_s_memory_and_each_process_s(
    tmp_path: Path, children: None
) -> None:
    proc = fake_proc(tmp_path / "proc")
    snapshot(tmp_path, NOW - timedelta(seconds=4), {"mode": "paper", "rss_mb": 182.4})

    said = await heartbeat.readings(tmp_path, lambda: NOW, proc)

    assert said == (
        "memory available 812 MB, swap used 20 MB; "
        "rss engine 431 MB, trader 182 MB, job prevalidate-cox 300 MB, training 640 MB"
    )


async def test_the_trader_s_memory_is_unknown_while_it_does_not_run(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc")

    async def trader() -> str:
        said = await heartbeat.readings(tmp_path, lambda: NOW, proc)
        found = re.search(r", trader (\S+) MB", said)
        assert found is not None, said
        return found.group(1)

    assert await trader() == "?", "no store"
    TraderStore(tmp_path / DB_NAME).close()
    assert await trader() == "?", "no snapshot yet"
    snapshot(tmp_path, NOW - timedelta(seconds=4), {"mode": "paper"})
    assert await trader() == "?", "a snapshot written before #547"
    snapshot(tmp_path, NOW - timedelta(seconds=16), {"mode": "paper", "rss_mb": 182.4})
    assert await trader() == "?", "older than ALIVE_SECONDS: the trader is not running"
    snapshot(tmp_path, NOW - timedelta(seconds=15), {"mode": "paper", "rss_mb": 182.4})
    assert await trader() == "182"
    assert heartbeat.line(None, {}, proc).endswith("rss engine 431 MB, trader ? MB")
    assert "trader ? MB" in await heartbeat.readings(None, lambda: NOW, proc), "no state folder"


def test_a_store_without_its_tables_is_no_figure(tmp_path: Path) -> None:
    (tmp_path / DB_NAME).write_bytes(b"")  # being made: an empty file
    assert read_resident(tmp_path / DB_NAME, NOW) is None


async def test_without_proc_every_figure_is_unknown(tmp_path: Path, children: None) -> None:
    said = await heartbeat.readings(tmp_path, lambda: NOW, tmp_path / "no-proc")
    assert said == (
        "memory available ? MB, swap used ? MB; "
        "rss engine ? MB, trader ? MB, job prevalidate-cox ? MB, training ? MB"
    )


def test_the_trader_writes_its_memory_into_its_snapshot_and_not_into_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "resident_mb", lambda: 182.4)

    Day(tmp_path).run([Tick("10:00:00")])

    assert read_resident(tmp_path / DB_NAME, at("10:00:00")) == 182.4
    assert read_resident(tmp_path / DB_NAME, at("10:00:16")) is None, "the trader stopped"
    state = read_trader(tmp_path / DB_NAME, at("10:00:00"))
    assert state["alive"] and state["mode"] is not None
    assert "rss_mb" not in state and "rss_mb" not in TRADER_FIELDS, "not a field of GET /v1/trader"


# The patterns of the docs' LogQL (``| regexp``), as Python reads them (RE2 reads these alike).
DOCUMENTED = {
    "available": r"available (?P<mb>\d+) MB",
    "swap": r"swap used (?P<mb>\d+) MB",
    "engine": r"rss engine (?P<mb>\d+) MB",
    "trader": r", trader (?P<mb>\d+) MB",
    "training": r", training (?P<mb>\d+) MB",
    "job": r", job (?P<job>\S+) (?P<mb>\d+) MB",
}


async def test_the_line_reaches_loki_as_the_docs_query_it(
    fake_loki: FakeLoki, tmp_path: Path, children: None
) -> None:
    proc = fake_proc(tmp_path / "proc")
    snapshot(tmp_path, NOW, {"mode": "paper", "rss_mb": 182.4})
    handler, _ = loki.start(fake_loki.values(), (), loki.ENGINE, "", formatter(), every=0.05)
    assert handler is not None
    fields = partial(heartbeat.readings, tmp_path, lambda: NOW, proc)
    beating = asyncio.create_task(loki.keep_heartbeat(0.01, fields=fields))
    try:
        await until(lambda: any("heartbeat: sent" in line for _, line in fake_loki.lines()))
    finally:
        beating.cancel()
    line = next(line for _, line in fake_loki.lines() if "heartbeat: sent" in line)
    assert re.search(
        r" INFO krx_quant\.loki: heartbeat: sent \d+, dropped 0, buffered \d+; memory available 812 MB,"
        r" swap used 20 MB; rss engine 431 MB, trader 182 MB, job prevalidate-cox 300 MB, training 640 MB$",
        line,
    ), line
    found = {name: re.search(pattern, line) for name, pattern in DOCUMENTED.items()}
    assert {name: m.group("mb") if m else None for name, m in found.items()} == {
        "available": "812",
        "swap": "20",
        "engine": "431",
        "trader": "182",
        "training": "640",
        "job": "300",
    }
    job = found["job"]
    assert job is not None and job.group("job") == "prevalidate-cox"
    # A job named like the training or the trader is not read as either.
    other = heartbeat.line(None, {JOB_PID: "job trader-report", TRAINING_PID: "job pretraining"}, proc)
    assert other.endswith("trader ? MB, job trader-report 300 MB, job pretraining 640 MB")
    assert re.search(DOCUMENTED["trader"], other) is None
    assert re.search(DOCUMENTED["training"], other) is None
