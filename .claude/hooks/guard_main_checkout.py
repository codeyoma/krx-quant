#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash): keep the shared main checkouts as they are (#325).

Several sessions work in this repository at once. The main checkout (the repository root) and
its `fincept/` submodule belong to whoever opened them; each session works in its own
worktree. This hook refuses, in a main checkout:

- switching or creating a branch (`git switch`, `git checkout`), except plainly switching back
  to the default branch (`main`, the fork's `custom`), without `-f`, `--detach` or the like;
- deleting several branches at once (`git branch -D a b`, or `git branch -D` fed by xargs);
- discarding work (`git reset --hard`, `git clean -f`, `git stash`).

Anything else, and anything in a worktree, passes. Each `git` invocation in the command is
judged in the checkout (`git rev-parse --show-toplevel`) it acts in: the session's working
directory, moved by any `cd <dir>` before it and by its own `-C` and `--work-tree`, after all of
git's global options (`-c`, `--no-pager`, ...). The main checkout is found from the repository's
common git dir, so the hook protects it whichever worktree a session runs in.

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


# Git's global options that take a separate value, and the ones that point it at another
# repository or work tree.
VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}
SEGMENT = re.compile(r"&&|\|\||;|\||\n")


def _resolve(path: str, cwd: Path) -> Path:
    expanded = Path(os.path.expanduser(os.path.expandvars(path)))
    return (expanded if expanded.is_absolute() else cwd / expanded).resolve()


def _words(segment: str) -> list[str]:
    segment = segment.strip().strip("()").strip()
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def git_calls(command: str, cwd: str) -> list[tuple[Path, str, list[str]]]:
    """Each `git` invocation in ``command``: the directory it acts in, its subcommand and the
    subcommand's arguments. Segments run in order, and a `cd <dir>` moves the ones after it;
    each invocation's own `-C` (several apply in turn) and `--work-tree` come on top. Every
    global option before the subcommand is consumed."""
    here = Path(cwd).resolve()
    calls = []
    for segment in SEGMENT.split(command):
        words = _words(segment)
        if words[:1] in (["cd"], ["pushd"]) and len(words) > 1:
            here = _resolve(words[1], here)
            continue
        if "git" not in words:
            continue
        args = words[words.index("git") + 1 :]
        place = here
        while args and args[0].startswith("-"):
            option, _, inline = args[0].partition("=")
            value = inline if inline else (args[1] if option in VALUE_OPTIONS and len(args) > 1 else "")
            if option == "-C" and value:
                place = _resolve(value, place)
            elif option == "--work-tree" and value:
                place = _resolve(value, here)
            args = args[1:] if inline or option not in VALUE_OPTIONS else args[2:]
        verb, rest = (args[0], args[1:]) if args else ("", [])
        calls.append((place, verb, rest))
    return calls


def _checkout(place: Path) -> Path:
    top = _git(place, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else place


QUIET = {"-q", "--quiet"}


def refusal(command: str, cwd: str, checkouts: dict[Path, str]) -> str | None:
    """Why ``command`` would change a shared main checkout, or None. Each `git` invocation is
    judged in the checkout it acts in; a worktree or another repository is free."""
    for place, verb, rest in git_calls(command, cwd):
        target = _checkout(place)
        default = checkouts.get(target)
        if default is None:
            continue
        if verb in ("switch", "checkout"):
            if set(rest) - QUIET == {default} and default in rest:
                continue  # back to the default branch, plainly: no -f, --detach or new branch
            return f"`git {verb}` changes the shared main checkout {target}."
        if verb == "branch" and ({"-D", "-d", "--delete"} & set(rest)):
            names = [a for a in rest if not a.startswith("-")]
            if len(names) != 1 or re.search(r"xargs[^|;&]*\bgit\b[^|;&]*\bbranch\b", command):
                return f"deleting several branches at once in {target} can take other sessions' branches."
        if verb == "reset" and "--hard" in rest:
            return f"`git reset --hard` discards work in the shared main checkout {target}."
        if verb == "clean" and any(a.startswith("-") and not a.startswith("--") and "f" in a for a in rest):
            return f"`git clean -f` deletes files in the shared main checkout {target}."
        if verb == "clean" and "--force" in rest:
            return f"`git clean --force` deletes files in the shared main checkout {target}."
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
