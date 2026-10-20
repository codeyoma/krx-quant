"""Fail if tracked files contain KIS order code, secrets or a PR number placeholder.

The engine is signals-only: no KIS order TR IDs or order endpoints may ever
appear in the repository. Secrets belong in `.env` on the server only. The patterns are
in `src/krx_quant/forbidden.py`; the scheduled jobs mask them out of what they post (#215).

Usage: python scripts/check_forbidden.py [paths...]   (default: all git-tracked files)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from krx_quant.forbidden import REPOSITORY_RULES, RULES

SELF = Path(__file__).resolve()
# The rules themselves, shared with what the server posts to the issue tracker (#215).
RULES_FILE = SELF.parents[1] / "src" / "krx_quant" / "forbidden.py"


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
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in (*RULES, *REPOSITORY_RULES):
            if pattern.search(line):
                hits.append(f"{path}:{lineno}: {name}")
    return hits


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
