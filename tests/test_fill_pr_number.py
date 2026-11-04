"""The PR number filler (``scripts/fill_pr_number.py``, #507): it puts a pull request's number in
place of the placeholders in the files its branch changed, and nowhere else.

It runs on a real repository made here, with an ``origin/main`` to branch from. The placeholders
are put together at run time, so the repository check does not find them in this file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fill_pr_number.py"
CHECK = SCRIPT.with_name("check_forbidden.py")
HASH = "#"
NOTE = f"> **바뀜** (2026-11-04, {HASH}PRNUM): 이전 → 지금. 이유: ….\n"


# Whatever this machine's git configuration: a name, and no signing.
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fill(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, timeout=20
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A branch off ``origin/main``. ``main`` holds a placeholder in a file the branch leaves alone:
    the check keeps one out of main, but the filler must not look there anyway."""
    root = tmp_path / "krx-quant"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / "docs" / "operations.md", "# 운영\n\n본문.\n")
    write(root / "docs" / "design.md", f"# 설계\n\n{NOTE}")
    write(root / "README.md", "# krx-quant\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "start")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "switch", "-q", "-c", "mine")
    return root


def test_it_fills_the_files_the_branch_changed_committed_or_not(repo: Path) -> None:
    write(repo / "docs" / "operations.md", f"# 운영\n\n본문.\n\n{NOTE}\n{NOTE}")
    git(repo, "commit", "-qam", "notes")
    write(repo / "README.md", f"# krx-quant\n\n{NOTE}")  # not committed yet
    write(repo / "src" / "krx_quant" / "jobs.py", f'NOTE = "ran after {HASH}NNN merged"\n')
    git(repo, "add", "src")  # a new file, staged
    write(repo / "notes.md", NOTE)  # untracked: not the branch's

    filled = fill(repo, "512")
    assert filled.returncode == 0, filled.stderr
    assert (repo / "docs" / "operations.md").read_text(encoding="utf-8").count("(2026-11-04, #512)") == 2
    assert "(2026-11-04, #512)" in (repo / "README.md").read_text(encoding="utf-8")
    jobs = (repo / "src" / "krx_quant" / "jobs.py").read_text(encoding="utf-8")
    assert jobs == 'NOTE = "ran after #512 merged"\n'
    # Files the branch did not change keep theirs.
    assert (repo / "docs" / "design.md").read_text(encoding="utf-8").endswith(NOTE)
    assert (repo / "notes.md").read_text(encoding="utf-8") == NOTE
    # Each file and its lines.
    assert "docs/operations.md: 2 line(s) (5, 7)" in filled.stdout
    assert "README.md: 1 line(s) (3)" in filled.stdout
    assert "src/krx_quant/jobs.py: 1 line(s) (1)" in filled.stdout
    assert "#512 in 4 line(s) of 3 file(s)." in filled.stdout


def test_running_it_again_changes_nothing(repo: Path) -> None:
    write(repo / "docs" / "operations.md", NOTE)
    assert fill(repo, "512").returncode == 0
    once = (repo / "docs" / "operations.md").read_text(encoding="utf-8")
    again = fill(repo, "513")
    assert again.returncode == 0
    assert (repo / "docs" / "operations.md").read_text(encoding="utf-8") == once
    assert "No PR number placeholder to fill in the 1 file(s) this branch changed." in again.stdout


def test_another_repository_s_stand_in_is_left_and_named(repo: Path) -> None:
    fork = f"화면은 codeyoma/FinceptTerminal{HASH}FORKPR, 엔진은 {HASH}PRNUM.\n"
    write(repo / "docs" / "operations.md", fork)
    filled = fill(repo, "512")
    assert filled.returncode == 0
    text = (repo / "docs" / "operations.md").read_text(encoding="utf-8")
    assert text == f"화면은 codeyoma/FinceptTerminal{HASH}FORKPR, 엔진은 #512.\n"
    assert "left: docs/operations.md:1 (another repository's pull request)" in filled.stdout
    # The check still fails on it: the author fills it in.
    doc = str(repo / "docs" / "operations.md")
    check = subprocess.run([sys.executable, str(CHECK), doc], capture_output=True)
    assert check.returncode == 1


def test_the_filled_files_pass_the_check(repo: Path) -> None:
    stand_ins = ("PRNUM", "NNN", "XXX", "TBD", "__PR3__", "KQPR", "PR_DOCS")
    write(repo / "docs" / "operations.md", "".join(f"- 노트 ({HASH}{s}).\n" for s in stand_ins))
    path = str(repo / "docs" / "operations.md")
    assert subprocess.run([sys.executable, str(CHECK), path], capture_output=True).returncode == 1
    assert fill(repo, "512").returncode == 0
    assert (repo / "docs" / "operations.md").read_text(encoding="utf-8").count("(#512)") == len(stand_ins)
    assert subprocess.run([sys.executable, str(CHECK), path], capture_output=True).returncode == 0


def test_line_endings_and_binary_files_stay_as_they_are(repo: Path) -> None:
    crlf = repo / "docs" / "windows.md"
    crlf.write_bytes(f"첫 줄\r\n노트 ({HASH}PRNUM).\r\n".encode())
    blob = repo / "docs" / "chart.png"
    blob.write_bytes(b"\x89PNG\xff" + f"{HASH}PRNUM".encode())
    git(repo, "add", "docs")
    filled = fill(repo, "512")
    assert filled.returncode == 0, filled.stderr
    assert crlf.read_bytes() == "첫 줄\r\n노트 (#512).\r\n".encode()
    assert blob.read_bytes() == b"\x89PNG\xff" + f"{HASH}PRNUM".encode()
    assert "docs/windows.md: 1 line(s) (2)" in filled.stdout


def test_the_rules_file_keeps_its_examples(repo: Path) -> None:
    """``forbidden.py`` spells the placeholders out, so the check skips it, and so does the filler."""
    rules = repo / "src" / "krx_quant" / "forbidden.py"
    write(rules, f"# {HASH}PRNUM came to review that way.\n")
    filled = fill(repo, "512")
    assert filled.returncode == 0
    assert rules.read_text(encoding="utf-8") == f"# {HASH}PRNUM came to review that way.\n"


@pytest.mark.parametrize("arg", ["", "0", "-3", "PRNUM", "12a"])
def test_it_takes_a_pull_request_number(repo: Path, arg: str) -> None:
    write(repo / "docs" / "operations.md", NOTE)
    refused = fill(repo, arg)
    assert refused.returncode == 2
    assert (repo / "docs" / "operations.md").read_text(encoding="utf-8") == NOTE


def test_it_stops_without_origin_main(repo: Path) -> None:
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    write(repo / "docs" / "operations.md", NOTE)
    refused = fill(repo, "512")
    assert refused.returncode == 2
    assert "origin/main" in refused.stderr
    assert (repo / "docs" / "operations.md").read_text(encoding="utf-8") == NOTE
