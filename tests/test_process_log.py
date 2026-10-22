"""The processes the engine starts (#356, #358): their whole output read as it comes and sent to
Loki under their own labels, between a start line and one with the exit code; and a process's own
lines from its start to its end.

The engine's shipping is started as the engine starts it, never sending by time (``every`` 60 s):
what arrives, arrives because the child ended. The fake Loki is ``tests/loki_fake.py``'s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path

import aiohttp
import pytest

from krx_quant import loki
from krx_quant.clock import FakeClock
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.market_calendar import MarketCalendar
from krx_quant.process_log import BY_PARENT, FORMAT, Child, formatter, open_process_log, run_logged
from krx_quant.scheduled import Job, JobState, Scheduler, run_command
from krx_quant.scheduled import Target as IssueTarget
from krx_quant.swap import _train
from tests.loki_fake import FakeLoki
from tests.test_subscription_limit import until

NIGHT = date(2026, 10, 22)


def as_the_engine(fake: FakeLoki) -> None:
    handler, _ = loki.start(fake.values(), (), loki.ENGINE, "", formatter(), every=60.0)
    assert handler is not None


def of(fake: FakeLoki, process: str, fields: str) -> list[tuple[str, str]]:
    """The lines Loki took of ``process``, each ``(level, line)`` without ``fields``."""
    taken = [(labels, line) for labels, line in fake.lines() if labels["process"] == process]
    assert all(line.startswith(f"{fields} ") for _, line in taken), taken
    return [(labels["level"], line.removeprefix(f"{fields} ")) for labels, line in taken]


async def ended(fake: FakeLoki) -> None:
    await until(lambda: any(" ended: " in line or "stopped " in line for _, line in fake.lines()))


CHILD = (
    "import logging, sys\n"
    f"logging.basicConfig(level=logging.INFO, format={FORMAT!r})\n"
    "print('a report line', flush=True)\n"
    "logging.getLogger('krx_quant.training').info('step %s done', 'newton')\n"
    "print('no newline at the end', end='', flush=True)\n"
    "sys.exit(3)\n"
)


async def test_a_child_s_output_reaches_loki_between_its_start_and_its_exit_code(fake_loki: FakeLoki) -> None:
    as_the_engine(fake_loki)
    child = Child.job("probe-x", NIGHT)
    ran = await run_logged([sys.executable, "-c", CHILD], timedelta(seconds=20), child=child)
    assert ran.exit_code == 3 and not ran.timed_out
    assert ran.stdout == "a report line\nno newline at the end"
    assert ran.stderr.endswith("INFO krx_quant.training: step newton done\n")
    await ended(fake_loki)
    first, *middle, last = of(fake_loki, "job", "job=probe-x night=2026-10-22")
    assert first[0] == "info" and f" INFO krx_quant.process_log: {sys.executable} -c " in first[1]
    assert "started (pid " in first[1]
    assert sorted(middle) == sorted(
        [("info", "a report line"), ("info", "no newline at the end"), ("info", ran.stderr.strip())]
    )
    assert (
        last[0] == "error"
        and " ERROR krx_quant.process_log: " in last[1]
        and "ended: exit 3 after" in last[1]
    )


async def test_a_child_stopped_at_its_time_limit_says_so(fake_loki: FakeLoki) -> None:
    as_the_engine(fake_loki)
    script = "import time; print('started', flush=True); time.sleep(30)"
    ran = await run_logged(
        [sys.executable, "-c", script], timedelta(seconds=1), merge=True, child=Child.training(NIGHT)
    )
    assert ran.timed_out and ran.stdout == "started\n"
    await ended(fake_loki)
    lines = of(fake_loki, "training", "night=2026-10-22")
    assert lines[1] == ("info", "started")
    assert lines[-1][0] == "error"
    assert "stopped at its time limit (0:00:01): exit -9 (SIGKILL) after" in lines[-1][1]


async def test_a_child_is_told_the_engine_sends_its_output() -> None:
    script = f"import os; print(os.environ.get({BY_PARENT!r}))"
    ran = await run_logged([sys.executable, "-c", script], timedelta(seconds=20))
    assert ran.stdout == "1\n"


async def test_without_loki_the_output_still_comes_back_whole() -> None:
    assert loki.active() is None
    ran = await run_logged([sys.executable, "-c", CHILD], timedelta(seconds=20), child=Child.training(NIGHT))
    assert (ran.exit_code, ran.stdout) == (3, "a report line\nno newline at the end")


@pytest.mark.timeout(60)  # the real command's imports (pyarrow, lightgbm) take a few seconds
async def test_the_nightly_training_s_whole_output_and_exit_code_reach_loki_and_not_from_itself(
    fake_loki: FakeLoki, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real `krx-quant train`, which stops at once without an Object Store (exit 2). It has the
    # Loki settings too: it would send its own lines (as a command) if it were not told.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KRX_OBJECT_STORE", raising=False)
    for name, value in fake_loki.values().items():
        monkeypatch.setenv(name, value)
    as_the_engine(fake_loki)
    failure = await _train(NIGHT)
    assert failure is not None and failure.exit_code == 2
    await ended(fake_loki)
    lines = of(fake_loki, "training", "night=2026-10-22")
    assert "INFO krx_quant.process_log: krx-quant train --day 2026-10-22 started (pid" in lines[0][1]
    assert any("configuration error: KRX_OBJECT_STORE is not set" in line for _, line in lines)
    assert lines[-1][0] == "error" and "krx-quant train --day 2026-10-22 ended: exit 2 after" in lines[-1][1]
    assert await asyncio.to_thread(loki.stop) == 0
    assert not [line for labels, line in fake_loki.lines() if labels["process"] == "command"], (
        "the child sent nothing itself"
    )


async def test_the_training_s_steps_and_the_kill_that_ended_it_reach_loki(fake_loki: FakeLoki) -> None:
    # #355's step lines, logged in the train process as nightly.py logs them, then the OOM kill.
    steps = (
        "import logging, os, signal\n"
        f"logging.basicConfig(level=logging.INFO, format={FORMAT!r})\n"
        "log = logging.getLogger('krx_quant.nightly')\n"
        "log.info('started step %s', 'newton')\n"
        "log.info('%s: %.1f s, peak %s MB (since the %s), %d days read, %d days cached',"
        " 'newton', 41.2, 812, 'step', 23, 3)\n"
        "log.info('started step %s', 'turing')\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    as_the_engine(fake_loki)
    failure = await _train(NIGHT, [sys.executable, "-c", steps])
    assert failure is not None and (failure.exit_code, failure.step) == (-9, "turing")
    await ended(fake_loki)
    first, *middle, last = of(fake_loki, "training", "night=2026-10-22")
    assert "started (pid " in first[1]
    assert [line.split(": ", 1)[1] for _, line in middle] == [
        "started step newton",
        "newton: 41.2 s, peak 812 MB (since the step), 23 days read, 3 days cached",
        "started step turing",
    ]
    assert last[0] == "error" and "ended: exit -9 (SIGKILL) after" in last[1]


TRACEBACK = (
    "import logging, sys\n"
    f"logging.basicConfig(level=logging.INFO, format={FORMAT!r})\n"
    "log = logging.getLogger('krx_quant.nightly')\n"
    "log.info('started step %s', 'newton')\n"
    "print('a report line', flush=True)\n"
    "try:\n"
    "    1 / 0\n"
    "except ZeroDivisionError:\n"
    "    log.exception('step %s failed', 'turing')\n"
    "sys.exit(3)\n"
)


async def test_a_traceback_s_lines_take_the_error_s_level(fake_loki: FakeLoki) -> None:
    as_the_engine(fake_loki)
    ran = await run_logged(
        [sys.executable, "-c", TRACEBACK], timedelta(seconds=20), merge=True, child=Child.training(NIGHT)
    )
    assert ran.exit_code == 3
    await ended(fake_loki)
    lines = of(fake_loki, "training", "night=2026-10-22")
    assert lines[1][0] == "info" and lines[1][1].endswith("INFO krx_quant.nightly: started step newton")
    assert lines[2] == ("info", "a report line")
    failed = next(i for i, (_, text) in enumerate(lines) if "step turing failed" in text)
    assert lines[failed][0] == "error"
    assert [level for level, _ in lines[failed + 1 : -1]] == ["error"] * (len(lines) - failed - 2)
    assert [text for _, text in lines[1:-1]] == ran.stdout.splitlines(), "Loki has what it printed"


async def test_a_scheduled_job_s_run_reaches_loki_as_process_job(
    fake_loki: FakeLoki, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)  # the engine's own lines, as the engine logs them
    as_the_engine(fake_loki)
    calendar = MarketCalendar({NIGHT: True, NIGHT + timedelta(days=1): True})
    clock = FakeClock(datetime.combine(NIGHT, time(23, 0), KST))
    script = "import sys; print('found 3 days'); print('a warning', file=sys.stderr)"
    job = Job("probe-x", lambda night: True, ("-c", script), timedelta(minutes=1), (IssueTarget(1),))
    async with aiohttp.ClientSession() as session:

        async def told(text: str) -> bool:
            return True

        scheduler = Scheduler(
            [job],
            time(22, 30),
            calendar,
            JobState.load(tmp_path / "scheduled.json"),
            GitHub(session, None, "codeyoma/krx-quant", "http://127.0.0.1:9"),
            told,
            clock,
            runner=partial(run_command, program=[sys.executable]),
        )
        await scheduler.run_night(NIGHT)
    await ended(fake_loki)  # sent at its end
    assert await asyncio.to_thread(loki.stop) == 0  # and the engine's own lines after it
    first, *middle, last = of(fake_loki, "job", "job=probe-x night=2026-10-22")
    assert "started (pid " in first[1] and "ended: exit 0 after" in last[1]
    assert sorted(text for _, text in middle) == ["a warning", "found 3 days"]
    engine = [line for labels, line in fake_loki.lines() if labels["process"] == "engine"]
    assert any("scheduled job probe-x: ok (exit 0)" in line for line in engine), "the engine's own, apart"


# A process's own lines


def test_a_process_the_engine_started_or_without_loki_sends_nothing(
    fake_loki: FakeLoki, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert open_process_log("doctor", ["doctor"], {}) is None
    monkeypatch.setenv(BY_PARENT, "1")
    assert open_process_log("doctor", ["doctor"], fake_loki.values()) is None
    assert loki.active() is None


async def test_a_process_s_own_lines_go_between_its_start_and_its_end(
    fake_loki: FakeLoki, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    values = {**fake_loki.values(), "KRX_COMMIT": "abcdef0123456789"}
    files = open_process_log("collect", ["collect"], values)
    assert files is not None and loki.active() is not None
    logging.getLogger("krx_quant.test").warning("a line of %s", "collect")
    await asyncio.to_thread(files.close, 0)
    assert loki.active() is None
    taken = [(labels["process"], labels["level"], line) for labels, line in fake_loki.lines()]
    assert {process for process, _, _ in taken} == {"engine"}
    assert taken[0][2].endswith(f"krx-quant collect started (pid {os.getpid()}, commit abcdef012345)")
    assert taken[1][2].endswith(
        "INFO krx_quant.process_log: sending the log lines to Loki too (process=engine)"
    )
    assert taken[2][1:] == ("warning", taken[2][2]) and taken[2][2].endswith(
        "WARNING krx_quant.test: a line of collect"
    )
    assert " INFO krx_quant.process_log: krx-quant collect ended: exit 0 after" in taken[-1][2]
    assert "krx-quant collect started" not in caplog.text, "the start and end lines go to Loki only"
