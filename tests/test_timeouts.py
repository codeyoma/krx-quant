"""A test past its time limit ends the run, wherever it is stuck (pytest-timeout's ``thread``
method, ``pyproject.toml``).

The ``signal`` method raises the timeout inside the main thread, wherever it happens to be. When
that is an asyncio callback, such as the one that hands a worker thread's result back to the
loop, asyncio logs the exception and carries on: the future the test awaits never completes, the
alarm does not fire again, and the run waits forever at 0% CPU.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

STUCK = """
import asyncio
import time


async def test_stuck() -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def deliver() -> None:
        time.sleep(5)  # the time limit runs out inside this callback
        done.set_result(None)

    loop.call_soon(deliver)
    await done
"""


@pytest.mark.timeout(120)  # a pytest run of its own, which a loaded machine starts slowly
def test_a_test_stuck_in_an_asyncio_callback_ends_the_run_with_its_stack(tmp_path: Path) -> None:
    (tmp_path / "test_stuck.py").write_text(STUCK)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(ROOT / "pyproject.toml"),
        "--rootdir",
        str(tmp_path),
        "-o",
        "timeout=1",
        "test_stuck.py",
    ]
    try:
        run = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        pytest.fail("the run went on waiting after the test's time limit", pytrace=False)

    assert run.returncode == 1, run.stdout + run.stderr
    assert "Timeout" in run.stdout
    assert "in deliver" in run.stdout, "the stack shows where the test was stuck"
