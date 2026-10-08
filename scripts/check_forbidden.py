"""Fail if tracked files contain KIS order code or secrets.

The engine is signals-only: no KIS order TR IDs or order endpoints may ever
appear in the repository. Secrets belong in `.env` on the server only.

Usage: python scripts/check_forbidden.py [paths...]   (default: all git-tracked files)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SELF = Path(__file__).resolve()

RULES: list[tuple[str, re.Pattern[str]]] = [
    # KIS write/order TR IDs are four letters, four digits and "U" (reads end in "R"),
    # e.g. TTTC0802U, VTTC0801U, TTTT1002U, CTSC0008U, OTFM3001U, STTN1101U.
    ("KIS order TR ID", re.compile(r"\b[A-Z]{4}\d{4}U\b")),
    ("KIS order endpoint", re.compile(r"/trading/order|\border-(?:cash|credit|rvsecncl|resv)\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("JWT / KIS access token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("KIS credential value", re.compile(r"KIS_APP_(?:KEY|SECRET)\s*=\s*['\"]?[A-Za-z0-9+/=]{16,}")),
]


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


def scan(path: Path) -> list[str]:
    if path.resolve() == SELF or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in RULES:
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
