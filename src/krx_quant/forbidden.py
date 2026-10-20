"""Patterns that must never appear in the repository (``scripts/check_forbidden.py``), nor in
what the server posts to the public issue tracker (``redaction.py``, #215). ``REPOSITORY_RULES``
are the repository check's alone (#321).

The engine is signals-only: no KIS order TR IDs or order endpoints, and no Toss order
endpoints (#235), may ever appear in the repository. Secrets belong in ``.env`` on the server
only. This file spells the patterns out, so the repository check skips it.
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
    # The Toss Open API also takes orders with the same client (#235): its order and
    # conditional-order endpoints may never appear either, nor its client's values.
    ("Toss order endpoint", re.compile(r"/api/v1/(?:conditional-)?orders\b")),
    ("Toss credential value", re.compile(r"TOSS_CLIENT_(?:ID|SECRET)\s*=\s*['\"]?[A-Za-z0-9_+/=-]{16,}")),
    # Classic (ghp_, gho_, ...) and fine-grained (github_pat_) GitHub tokens (#215).
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
]

# The repository check alone (#321), not the redaction of posted output: a change note's PR
# number is only known once the PR is open, so a note is written with a stand-in first. One
# left in a merged PR names no change: #PRNUM, #NNN, #PRB, #PRDOCS and #__PR3__ each came to
# review that way. A real number (#123) and the format's own "#PR" in AGENTS.md pass.
REPOSITORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "PR number placeholder",
        re.compile(r"#(?:PR[A-Z0-9_]+|N{3,}|X{3,}|TBD|_+PR[A-Za-z0-9_]*_+)(?![A-Za-z0-9_])"),
    ),
]
