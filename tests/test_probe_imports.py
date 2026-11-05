"""The live entry points load no probe (#552): the collector, the engine and the trader import no
``*_probe`` module, so a probe can be changed or deleted without breaking them. What a probe shares
with live code lives in the live module, and the probe imports it from there (``health.read_gaps``,
``net_assets.PRICE``, ``krx_net_assets.PATH``).

Input: a fresh interpreter importing one module. Output: the probe modules it loaded.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# What the server's processes run: ``krx-quant collect`` runs the collector, which serves the engine
# API, and ``krx-quant trade`` the trader, both through ``cli``. ``cli`` imported every probe for its
# commands; since #578 a command imports its modules only as it runs.
ENTRY_POINTS = ("krx_quant.cli", "krx_quant.collector", "krx_quant.engine", "krx_quant.trader.run")


def probes_loaded_by(module: str) -> list[str]:
    """The ``krx_quant`` probe modules (``*_probe``) a fresh interpreter has once it imported
    ``module``."""
    script = (
        f"import sys, {module}; "
        "print(*sorted(m for m in sys.modules if m.startswith('krx_quant.') and m.endswith('_probe')))"
    )
    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    return ran.stdout.split()


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_live_entry_point_loads_no_probe_module(entry: str) -> None:
    assert probes_loaded_by(entry) == []


def test_the_check_sees_a_probe_once_one_is_imported() -> None:
    # The same check finds a probe where one is imported: its pattern still names them.
    assert "krx_quant.nav_probe" in probes_loaded_by("krx_quant.nav_probe")
