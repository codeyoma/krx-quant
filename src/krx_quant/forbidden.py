"""Patterns that must never appear in the repository (``scripts/check_forbidden.py``), nor in
what the server posts to the public issue tracker (``redaction.py``, #215).

The engine is signals-only: no KIS order TR IDs or order endpoints may ever appear in the
repository. Secrets belong in ``.env`` on the server only. This file spells the patterns
out, so the repository check skips it.
"""

from __future__ import annotations

import re

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
    # Classic (ghp_, gho_, ...) and fine-grained (github_pat_) GitHub tokens (#215).
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
]
