"""The Claude Code hook that keeps the shared main checkouts as they are (#325).

It runs on a real repository made here: a main checkout on `main`, a `fincept` checkout on
`custom`, and a worktree beside them.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard_main_checkout.py"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_main_checkout", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_hook()


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def repository(path: Path, branch: str) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start")
    return path


@pytest.fixture
def place(tmp_path: Path) -> dict[str, Path]:
    root = repository(tmp_path / "krx-quant", "main")
    repository(root / "fincept", "custom")  # a nested checkout, like the submodule
    worktree = tmp_path / "scratch" / "kq"
    git(root, "worktree", "add", "-q", "-b", "mine", str(worktree))
    return {"root": root, "fork": root / "fincept", "worktree": worktree}


def refused(command: str, cwd: Path, root: Path) -> str | None:
    reason: str | None = guard.refusal(command, str(cwd), guard.protected(root))
    return reason


@pytest.mark.parametrize(
    "command",
    [
        "git switch docs/306-flow-screen",
        "git checkout feat/hover-window-border",
        "git switch -c mine2",
        "git checkout -b mine2",
        "git checkout -- README.md",
        "git branch --format='%(refname:short)' | grep -vx main | xargs git branch -D",
        "git branch -D one two",
        "git reset --hard origin/main",
        "git clean -fd",
        "git stash",
    ],
)
def test_the_main_checkout_refuses_what_changes_or_discards_its_state(
    command: str, place: dict[str, Path]
) -> None:
    assert refused(command, place["root"], place["root"]) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git switch main",  # back to its default branch
        "git pull -q --ff-only",
        "git branch -D my-merged-branch",  # one branch: a session's own, after its merge
        "git worktree add -q /tmp/x -b new origin/main",
        "git status --short",
        "git log --oneline -3",
        "uv run pytest -q",
    ],
)
def test_the_main_checkout_lets_reads_updates_and_single_cleanups_through(
    command: str, place: dict[str, Path]
) -> None:
    assert refused(command, place["root"], place["root"]) is None


def test_the_fork_checkout_is_protected_and_may_go_back_to_custom(place: dict[str, Path]) -> None:
    root, fork = place["root"], place["fork"]
    assert refused("git switch feat/x", fork, root) is not None
    assert refused("git switch custom", fork, root) is None
    assert refused(f"git -C {fork} switch feat/x", root, root) is not None
    assert refused(f"cd {fork} && git checkout feat/x", place["worktree"], root) is not None


def test_a_worktree_is_free(place: dict[str, Path]) -> None:
    root, worktree = place["root"], place["worktree"]
    for command in ("git switch -c other", "git reset --hard origin/main", "git stash", "git branch -D a b"):
        assert refused(command, worktree, root) is None
    assert refused(f"git -C {worktree} switch -c other", root, root) is None


def test_a_subdirectory_of_the_main_checkout_counts_as_the_main_checkout(place: dict[str, Path]) -> None:
    sub = place["root"] / "src"
    sub.mkdir()
    assert refused("git switch feat/x", sub, place["root"]) is not None


def test_the_main_checkout_is_found_from_a_worktree(place: dict[str, Path]) -> None:
    assert guard.main_root(place["worktree"]) == place["root"].resolve()


def test_the_hook_exits_2_with_its_reason_and_0_otherwise(place: dict[str, Path]) -> None:
    """Run as Claude Code runs it: JSON on stdin, from inside the made repository."""
    hook = place["root"] / ".claude" / "hooks" / "guard_main_checkout.py"
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")

    def run(command: str) -> subprocess.CompletedProcess[str]:
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(place["root"])}
        return subprocess.run(
            [sys.executable, str(hook)], input=json.dumps(event), capture_output=True, text=True
        )

    blocked = run("git switch other")
    assert blocked.returncode == 2
    assert "worktree" in blocked.stderr
    assert run("git status").returncode == 0


def test_each_git_call_is_judged_in_its_own_checkout(place: dict[str, Path]) -> None:
    """A `-C` elsewhere earlier in the command does not cover a later call in the main checkout."""
    root, worktree = place["root"], place["worktree"]
    assert refused("git -C /tmp status; git reset --hard", root, root) is not None
    assert refused(f"git -C {worktree} status && git stash", root, root) is not None
    assert refused(f"cd {worktree} && git stash", root, root) is None
    assert refused(f"cd {worktree} && git status; cd {root} && git switch feat/x", root, root) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git -c advice.detachedHead=false switch feature",
        "git --no-pager checkout feature",
        "git -c core.pager=cat -C . reset --hard",
        "git --git-dir=.git --no-optional-locks stash",
    ],
)
def test_global_options_before_the_subcommand_are_consumed(command: str, place: dict[str, Path]) -> None:
    assert refused(command, place["root"], place["root"]) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git switch --detach main",
        "git checkout -f main",
        "git checkout --force main",
        "git switch --discard-changes main",
    ],
)
def test_going_back_to_the_default_branch_must_be_plain(command: str, place: dict[str, Path]) -> None:
    assert refused(command, place["root"], place["root"]) is not None
    assert refused("git switch -q main", place["root"], place["root"]) is None
