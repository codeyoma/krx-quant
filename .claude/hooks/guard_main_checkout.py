#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash): keep the shared main checkouts as they are (#325).

Several sessions work in this repository at once. The main checkout (the repository root) and
its `fincept/` submodule belong to whoever opened them; each session works in its own
worktree. This hook refuses, in a main checkout:

- switching or creating a branch (`git switch`, `git checkout`), except switching back to the
  default branch (`main`, the fork's `custom`);
- deleting several branches at once (`git branch -D a b`, or `git branch -D` fed by xargs);
- discarding work (`git reset --hard`, `git clean -f`, `git stash`).

Anything else, and anything in a worktree, passes. The target is the checkout (its
`git rev-parse --show-toplevel`) of a leading `cd <dir> &&`, else of a `git -C <dir>`, else of
the session's working directory. The main checkout is found from the repository's common git
dir, so the hook protects it whichever worktree a session runs in.

Input: the hook's JSON on stdin. A refusal prints the reason on stderr and exits 2, which
Claude Code shows to the model; a pass exits 0.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def _git(path: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def main_root(here: Path) -> Path:
    """The main checkout: the parent of the repository's common git dir, whichever worktree
    this hook file was read from."""
    common = _git(here, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).resolve().parent if common else here.resolve().parents[1]


def protected(root: Path) -> dict[Path, str]:
    """The main checkouts and the default branch each may be switched back to."""
    return {root.resolve(): "main", (root / "fincept").resolve(): "custom"}


WORKTREE_HINT = (
    "Work in your own worktree instead, e.g. "
    "`git -C {root} worktree add <scratchpad>/<name> -b <branch> origin/main` "
    "(for the fork: `git -C {root}/fincept worktree add ... origin/custom`). "
    'See AGENTS.md "Shared checkouts".'
)


def target_dir(command: str, cwd: str) -> Path:
    lead = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", command)
    if lead:
        return _resolve(lead.group(1).strip("'\""), cwd)
    in_c = re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
    if in_c:
        return _resolve(in_c.group(1).strip("'\""), cwd)
    return Path(cwd).resolve()


def _resolve(path: str, cwd: str) -> Path:
    expanded = Path(os.path.expanduser(os.path.expandvars(path)))
    return (expanded if expanded.is_absolute() else Path(cwd) / expanded).resolve()


def git_args(command: str) -> list[list[str]]:
    """Each `git ...` invocation in ``command``, its arguments after `git` and any `-C dir`."""
    found = []
    for part in re.split(r"&&|\|\||;|\|", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if "git" not in words:
            continue
        args = words[words.index("git") + 1 :]
        while args[:1] == ["-C"]:
            args = args[2:]
        found.append(args)
    return found


def refusal(command: str, cwd: str, checkouts: dict[Path, str]) -> str | None:
    place = target_dir(command, cwd)
    top = _git(place, "rev-parse", "--show-toplevel")
    target = Path(top).resolve() if top else place
    default = checkouts.get(target)
    if default is None:
        return None  # a worktree, or not a checkout of this repository
    for args in git_args(command):
        verb, rest = (args[0], args[1:]) if args else ("", [])
        if verb in ("switch", "checkout"):
            targets = [a for a in rest if not a.startswith("-")]
            if not ({"-c", "-C", "-b", "-B", "--orphan", "--"} & set(rest)) and targets == [default]:
                continue  # back to the default branch
            return f"`git {verb}` changes the shared main checkout {target}."
        if verb == "branch" and ({"-D", "-d", "--delete"} & set(rest)):
            names = [a for a in rest if not a.startswith("-")]
            if len(names) != 1 or re.search(r"xargs[^|]*\bgit\b[^|]*\bbranch\b", command):
                return f"deleting several branches at once in {target} can take other sessions' branches."
        if verb == "reset" and "--hard" in rest:
            return f"`git reset --hard` discards work in the shared main checkout {target}."
        if verb == "clean" and any(a.startswith("-") and "f" in a for a in rest):
            return f"`git clean -f` deletes files in the shared main checkout {target}."
        if verb == "stash":
            return f"`git stash` takes other sessions' changes in the shared main checkout {target}."
    return None


def main() -> int:
    event = json.load(sys.stdin)
    command = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()
    root = main_root(Path(__file__).parent)
    reason = refusal(command, cwd, protected(root))
    if reason is None:
        return 0
    hint = WORKTREE_HINT.format(root=root)
    print(f"Refused by .claude/hooks/guard_main_checkout.py: {reason} {hint}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
