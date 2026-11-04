"""Put a pull request's number in place of the PR number placeholders its branch wrote (#507).

A change note cites its PR's number (AGENTS.md "Documentation"), which is known only once the PR
is open. So the note is written with a stand-in, the PR is opened as a draft, whose CI run skips
the placeholder rule (`scripts/check_forbidden.py --draft`), and this script then puts the number
in. Of the CI failures before it, 22 of 31 were placeholders on a branch's first push.

What it replaces is what the placeholder rule finds (its pattern is in
`src/krx_quant/forbidden.py`), in the files the branch changed against `origin/main`: committed,
staged or not, but not untracked files, nor the two files the check skips. A stand-in glued to a
name before it (`owner/repo#...`) names another repository's pull request: it is left as it is,
and listed, for its author to fill in. It prints each file it changed with its lines. Running it
again changes nothing.

Usage: uv run python scripts/fill_pr_number.py <PR number>   (run it in the branch's checkout)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from check_forbidden import PLACEHOLDER_PATTERN, ROOT, RULES_FILE, SELF

BASE = "origin/main"
# The files the check skips, from the repository root.
SKIPPED = frozenset({SELF.relative_to(ROOT).as_posix(), RULES_FILE.relative_to(ROOT).as_posix()})
# What may come right before another repository's reference (`owner/repo#12`): a name's characters.
GLUED = re.compile(r"[\w./-]")


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def changed_files(root: Path) -> list[str]:
    """The files the branch changed against ``origin/main`` and still has, from the root."""
    base = git(root, "merge-base", BASE, "HEAD").strip()
    listed = git(root, "diff", "--name-only", "-z", "--diff-filter=d", base)
    return sorted(path for path in listed.split("\0") if path and path not in SKIPPED)


def fill(text: str, number: int) -> tuple[str, list[int], list[int]]:
    """``text`` with this pull request's stand-ins replaced by ``#number``, the lines changed, and
    the lines whose stand-in names another repository's pull request (left as they are)."""
    lines, filled, left = [], [], []
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        others = False

        def replace(match: re.Match[str], line: str = line) -> str:
            nonlocal others
            if match.start() > 0 and GLUED.match(line[match.start() - 1]):
                others = True
                return match.group(0)
            return f"#{number}"

        new = PLACEHOLDER_PATTERN.sub(replace, line)
        if new != line:
            filled.append(lineno)
        if others:
            left.append(lineno)
        lines.append(new)
    return "".join(lines), filled, left


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not re.fullmatch(r"[1-9]\d*", argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    number = int(argv[0])
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        paths = changed_files(root)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    total_files = total_lines = 0
    for path in paths:
        # As bytes, so a file's line endings stay as they are.
        try:
            text = (root / path).read_bytes().decode("utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue  # a binary file, a submodule's directory
        new, filled, left = fill(text, number)
        if filled:
            (root / path).write_bytes(new.encode("utf-8"))
            total_files, total_lines = total_files + 1, total_lines + len(filled)
            print(f"{path}: {len(filled)} line(s) ({', '.join(map(str, filled))})")
        for lineno in left:
            print(f"left: {path}:{lineno} (another repository's pull request)")
    if total_lines:
        print(f"#{number} in {total_lines} line(s) of {total_files} file(s).")
    else:
        print(f"No PR number placeholder to fill in the {len(paths)} file(s) this branch changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
