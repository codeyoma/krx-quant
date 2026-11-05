"""Each ``krx-quant`` command loads only what it runs (#578).

``cli.py`` imported every command's modules whatever the command: ``krx-quant trade`` loaded
lightgbm and scipy, which the trader never uses, and took 136 MB against 104 MB for
``trader.run`` alone; ``notify`` took 136 MB against 67 (macOS, the repository's venv). The server
has 2 GB, and the trader runs all day.

Input: a fresh interpreter that runs a command in an empty directory, with Loki half set, so its
settings are read for the masking as on the server and nothing is sent. Output: the modules it
loaded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# What the trader never uses: the training's (LightGBM) and the experiments' (SciPy).
TRAINING = ("lightgbm", "scipy")
# What a notice needs none of: besides those, the engine, the algorithms, the trader and S3.
ENGINE = (*TRAINING, "boto3", "krx_quant.engine", "krx_quant.algorithms", "krx_quant.trader")

# The trader started as ``krx-quant trade`` starts it, but for its loop, which returns at once.
TRADE = """
import sys
from krx_quant.trader import run

async def stopped(settings, trader, *, stop):
    return None

run.run_trader = stopped
from krx_quant import cli
assert cli.main(["trade"]) == 0
print(*sys.modules)
"""
# Without Telegram set, the notice is only logged, and the command exits 1.
NOTIFY = """
import sys
from krx_quant import cli
assert cli.main(["notify", "a test notice"]) == 1
print(*sys.modules)
"""
PARSER = """
import sys
from krx_quant import cli
cli._parser()
print(*sys.modules)
"""


def loaded(script: str, cwd: Path) -> list[str]:
    """The modules a fresh interpreter has once it ran ``script`` in ``cwd``."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("KRX_", "KIS_", "TELEGRAM_", "AWS_", "TOSS_"))
    }
    env["KRX_LOKI_URL"] = "http://127.0.0.1:9/loki/api/v1/push"  # alone, nothing is sent
    ran = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout.split()


def among(modules: list[str], packages: tuple[str, ...]) -> list[str]:
    """Those of ``modules`` that are ``packages`` or in them."""
    return sorted(m for m in modules if any(m == p or m.startswith(f"{p}.") for p in packages))


def test_the_trader_loads_no_training_module(tmp_path: Path) -> None:
    modules = loaded(TRADE, tmp_path)

    assert "krx_quant.trader.run" in modules
    assert among(modules, TRAINING) == []


def test_a_notice_loads_neither_the_engine_nor_the_trader(tmp_path: Path) -> None:
    modules = loaded(NOTIFY, tmp_path)

    assert "krx_quant.notify" in modules
    assert among(modules, ENGINE) == []


def test_the_parser_loads_no_command_s_module(tmp_path: Path) -> None:
    # Every command's parser is built, the arguments' defaults too, and none of what they run.
    modules = loaded(PARSER, tmp_path)

    assert among(modules, (*ENGINE, "krx_quant.notify", "krx_quant.doctor")) == []
