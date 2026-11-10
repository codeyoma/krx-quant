"""The live entry points load no probe (#552): the collector, the engine and the trader import no
``*_probe`` module, so a probe can be changed or deleted without breaking them. What a probe shares
with live code lives in the live module, and the probe imports it from there (``health.read_gaps``,
``net_assets.PRICE``, ``krx_net_assets.PATH``).

And no live module imports a probe, a check or an experiment (#650): each is a leaf that only the
commands (``cli/``) and the scheduled jobs (``scheduled/``) that run it import, besides other leaves.
What one shares with live code lives in its owner (``base_rates.day_prices``,
``recording_quality.group_line``) and the leaf imports it from there.

Input: a fresh interpreter importing one module; the syntax trees of the package's modules. Output:
the probe modules it loaded; the imports of a leaf by a module that is not one.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import krx_quant

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


# The leaves: every probe (``*_probe``), check (``*_check``) and experiment (``*_experiment`` and the
# research reports named below), by module name under ``krx_quant``. A module named like one that is
# not a leaf is in ``NOT_LEAVES``, with why.
LEAVES = frozenset(
    {
        # Probes: what a source's fields mean, asked once (CODING_STANDARDS.md).
        "gaps_probe",
        "krx_etf_probe",
        "live_probe",
        "nav_probe",
        "night_probe",
        "order_probe",
        "sample_keys_probe",
        "close_restart_probe",
        "session_memory_probe",
        "supply_probe",
        "toss_probe",
        # Checks (``*_check``), a claim measured on the stored data: none since #658 removed the last
        # (``closing_flow_check``, ``daily_check``, ``structure_check``).
        # Experiments and research reports: a hypothesis judged, or a report an agent reads.
        "holdout",
        "inverse_replay",
        "smt_experiment",
        "verdict_replay",
    }
)
NOT_LEAVES = {
    # The mock and live trader compare each day's orders with KIS's by it (``trader.mock``): it is
    # the trader's execution check, not a one-off one.
    "trader.mock_check": "the trader's daily execution check",
}
# The packages whose modules run the leaves: the commands and the scheduled jobs.
RUNNERS = ("cli", "scheduled")
SOURCE = Path(krx_quant.__file__).parent


def module_name(path: Path) -> str:
    """``path``'s module under ``krx_quant``, dotted (a package by its name)."""
    parts = path.relative_to(SOURCE).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imported(tree: ast.Module) -> set[str]:
    """The ``krx_quant`` modules a syntax tree imports, by name under ``krx_quant``, wherever the
    import is (a command imports its modules in its handler): ``from krx_quant.x import y`` names
    ``x`` and ``x.y`` (``y`` may be a module), ``from krx_quant import x`` names ``x``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        else:
            continue
        found |= {n.removeprefix("krx_quant.") for n in names if n.startswith("krx_quant.")}
        if isinstance(node, ast.ImportFrom) and node.module == "krx_quant":
            found |= {a.name for a in node.names}
    return found


def modules() -> dict[str, ast.Module]:
    return {module_name(p): ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SOURCE.rglob("*.py"))}


def leaves_imported_by_live_modules(found: dict[str, ast.Module]) -> list[str]:
    """``importer -> leaf`` for each leaf a module imports that is neither a leaf nor a runner's."""
    return sorted(
        f"{name} -> {leaf}"
        for name, tree in found.items()
        if name not in LEAVES and name.split(".")[0] not in RUNNERS
        for leaf in sorted(imported(tree) & LEAVES)
    )


def test_no_live_module_imports_a_probe_a_check_or_an_experiment() -> None:
    assert leaves_imported_by_live_modules(modules()) == []


def test_every_module_named_as_a_probe_a_check_or_an_experiment_is_listed() -> None:
    named = {
        name for name in modules() if name.rsplit(".", 1)[-1].endswith(("_probe", "_check", "_experiment"))
    }
    assert named - LEAVES - NOT_LEAVES.keys() == set()
    assert LEAVES <= modules().keys(), "a leaf deleted leaves the list: take it out"


def test_the_rule_sees_a_live_module_that_imports_a_leaf() -> None:
    # The same check finds an import of a leaf, at the top or in a function, in either form.
    found = {
        "base_rates": ast.parse("from krx_quant.holdout import judge"),
        "collector": ast.parse("def f():\n    from krx_quant import gaps_probe"),
        "engine": ast.parse("import krx_quant.smt_experiment"),
        "cli.probes": ast.parse("from krx_quant.gaps_probe import probe_gaps"),
        "smt_experiment": ast.parse("from krx_quant.holdout import judge"),
    }
    assert leaves_imported_by_live_modules(found) == [
        "base_rates -> holdout",
        "collector -> gaps_probe",
        "engine -> smt_experiment",
    ]
