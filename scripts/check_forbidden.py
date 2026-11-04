"""Fail if tracked files contain KIS order code, secrets or a PR number placeholder.

The engine is signals-only: KIS order TR IDs and order endpoints may appear in the trader's KIS
account module alone (`src/krx_quant/kis_orders.py`, #393, #403), and nowhere else. Secrets
belong in `.env` on the server only. The patterns and that one exception are in
`src/krx_quant/forbidden.py`; the scheduled jobs mask every pattern out of what they post (#215).

A draft pull request's run passes `--draft` (#507): it skips the PR number placeholder rule alone,
and lists what it skipped. Its number is known only once it is open, and
`scripts/fill_pr_number.py` puts it in. Every other rule holds on a draft, and CI checks the
placeholders again once the pull request is marked ready, and on a push to `main`.

Usage: python scripts/check_forbidden.py [--draft] [paths...]   (default: all git-tracked files)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from krx_quant.forbidden import REPOSITORY_RULES, findings

SELF = Path(__file__).resolve()
ROOT = SELF.parents[1]
# The rules themselves, shared with what the server posts to the issue tracker (#215).
RULES_FILE = ROOT / "src" / "krx_quant" / "forbidden.py"
# The one rule a draft may break (#507), by its name in forbidden.py, which keeps the pattern;
# scripts/fill_pr_number.py replaces what it finds.
PLACEHOLDER = "PR number placeholder"
PLACEHOLDER_PATTERN = dict(REPOSITORY_RULES)[PLACEHOLDER]
DRAFT = "--draft"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


def scan(path: Path) -> list[tuple[str, str]]:
    """Each finding in ``path``: where (``path:line``) and the rule's name."""
    if path.resolve() in (SELF, RULES_FILE) or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    return [(f"{path}:{lineno}", name) for lineno, name in findings(from_root(path), text)]


def from_root(path: Path) -> str:
    """``path`` from the repository root, as ``git ls-files`` gives it; a file outside the
    repository by its full path (no exception of ``forbidden.ALLOWED_IN`` is its)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def main(argv: list[str]) -> int:
    draft = DRAFT in argv
    paths = [Path(a) for a in argv if a != DRAFT] or tracked_files()
    found = [hit for p in paths for hit in scan(p)]
    hits = [f"{where}: {name}" for where, name in found if not (draft and name == PLACEHOLDER)]
    waiting = [f"{where}: {name}" for where, name in found if draft and name == PLACEHOLDER]
    for hit in hits:
        print(hit)
    for hit in waiting:
        print(f"{hit} (skipped on a draft: it fails once the pull request is ready)")
    if waiting:
        print(
            f"::warning::{len(waiting)} PR number placeholder(s) left: run"
            " `uv run python scripts/fill_pr_number.py <PR>` before marking the pull request ready"
        )
    if hits:
        print(f"{len(hits)} forbidden pattern(s) found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
