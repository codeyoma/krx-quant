"""The repository check (``scripts/check_forbidden.py``) fails on a PR number placeholder (#321).

The placeholders here are put together at run time, so the check does not find them in this
file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from krx_quant.forbidden import REPOSITORY_RULES, RULES
from krx_quant.redaction import redact

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_forbidden.py"
HASH = "#"


def placeholder_hits(text: str) -> list[str]:
    return [name for name, pattern in REPOSITORY_RULES if pattern.search(text)]


@pytest.mark.parametrize(
    "stand_in",
    ["PRNUM", "NNN", "PRB", "PRDOCS", "__PR3__", "PR_NUMBER", "XXX", "TBD", "NNNN"],
)
def test_a_change_notes_stand_in_for_its_pr_number_is_found(stand_in: str) -> None:
    note = f"> **바뀜** (2026-10-20, {HASH}{stand_in}): 이전 → 지금. 이유: …."
    assert placeholder_hits(note) == ["PR number placeholder"]


@pytest.mark.parametrize(
    "text",
    [
        "> **바뀜** (2026-10-20, #317): 이전 → 지금.",  # a real PR number
        "Closes #321",
        "- `> **삭제됨** (YYYY-MM-DD, #PR): 무엇이 없어졌는지.`",  # the format itself (AGENTS.md)
        "cited by commit SHA instead of `#PR`.",
        "# PRNUM heading",  # a heading, not a reference
        "x = 1  # NNN in a comment after a space",
        "codeyoma/FinceptTerminal#97",
        "#PRs",  # lower case: prose, not a stand-in
    ],
)
def test_real_references_and_the_format_pass(text: str) -> None:
    assert placeholder_hits(text) == []


def test_the_check_fails_on_a_file_with_a_placeholder_and_passes_once_it_is_replaced(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(f"> **바뀜** (2026-10-20, {HASH}PRNUM): 이전 → 지금.\n", encoding="utf-8")
    failed = subprocess.run([sys.executable, str(SCRIPT), str(doc)], capture_output=True, text=True)
    assert failed.returncode == 1
    assert "notes.md:1: PR number placeholder" in failed.stdout

    doc.write_text("> **바뀜** (2026-10-20, #321): 이전 → 지금.\n", encoding="utf-8")
    passed = subprocess.run([sys.executable, str(SCRIPT), str(doc)], capture_output=True, text=True)
    assert passed.returncode == 0


def test_posted_output_is_not_masked_for_placeholders() -> None:
    """The placeholder rule is the repository's alone: what the server posts keeps it."""
    text = f"see {HASH}PRNUM"
    assert redact(text, ()) == text
    assert all(pattern.pattern != REPOSITORY_RULES[0][1].pattern for _, pattern in RULES)
