"""Fail if tracked files contain KIS order code, secrets or a PR number placeholder.

The engine is signals-only: KIS order TR IDs and order endpoints may appear in the trader's KIS
account module alone (`src/krx_quant/kis_orders.py`, #393, #403), and nowhere else. Secrets
belong in `.env` on the server only. The patterns and that one exception are in
`src/krx_quant/forbidden.py`; the scheduled jobs mask every pattern out of what they post (#215).

Usage: python scripts/check_forbidden.py [paths...]   (default: all git-tracked files)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from krx_quant.forbidden import findings

SELF = Path(__file__).resolve()
ROOT = SELF.parents[1]
# The rules themselves, shared with what the server posts to the issue tracker (#215).
RULES_FILE = ROOT / "src" / "krx_quant" / "forbidden.py"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


def scan(path: Path) -> list[str]:
    if path.resolve() in (SELF, RULES_FILE) or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    return [f"{path}:{lineno}: {name}" for lineno, name in findings(from_root(path), text)]


def from_root(path: Path) -> str:
    """``path`` from the repository root, as ``git ls-files`` gives it; a file outside the
    repository by its full path (no exception of ``forbidden.ALLOWED_IN`` is its)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or tracked_files()
    hits = [hit for p in paths for hit in scan(p)]
    for hit in hits:
        print(hit)
    if hits:
        print(f"{len(hits)} forbidden pattern(s) found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
