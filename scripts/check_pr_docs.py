"""Fail a pull request that changes src/ but none of the docs, unless its body says why (#507).

Every PR updates the docs for what it adds or changes (AGENTS.md "Documentation"). Only reviewers
checked that, and Codex's reviews flagged eight PRs that had left the docs behind. The docs here
are what other agents learn the system from: anything under docs/, README.md and .env.example.

A change to src/ that needs no doc (a refactor, a fix that changes no setting, command, field or
behaviour the docs describe) says so in a line of the body of its own:

    Docs: none (<reason>)

A line without a reason, or with the format's "<reason>" left in, does not count.

The check reads the pull request's files and body from the REST API, both as they are now: a
re-run after a body edit sees the new body. A renamed file counts by both its paths.

Usage: python3 scripts/check_pr_docs.py <owner/repo> <pr number>   (needs `gh` and GH_TOKEN)

Standard library only, so CI runs it without installing the project.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass

CODE = "src/"
DOCS_DIR = "docs/"
DOC_FILES = frozenset({"README.md", ".env.example"})
# A line of its own; the reason runs to the line's last ")", so it may hold parentheses itself.
EXEMPTION = re.compile(r"^[ \t]*Docs:[ \t]*none[ \t]*\((?P<reason>.*)\)[ \t]*$", re.IGNORECASE | re.MULTILINE)
# The format's own stand-in, "<reason>" or "<이유>", copied without a reason.
STAND_IN = re.compile(r"<[^<>]*>")
FORMAT = "Docs: none (<reason>)"


def is_doc(path: str) -> bool:
    return path.startswith(DOCS_DIR) or path in DOC_FILES


@dataclass(frozen=True)
class Verdict:
    code: list[str]  # changed files under src/
    docs: list[str]  # changed docs
    reason: str | None  # the first "Docs: none" line's reason, if one gives a reason
    empty: bool  # the body has a "Docs: none" line without a reason

    @property
    def passes(self) -> bool:
        return not self.code or bool(self.docs) or self.reason is not None


def reasons(body: str) -> list[str]:
    """The reasons of the body's "Docs: none" lines, stripped; empty for one that gives none."""
    found = []
    for match in EXEMPTION.finditer(body):
        reason = match.group("reason").strip()
        found.append("" if STAND_IN.fullmatch(reason) else reason)
    return found


def verdict(files: list[str], body: str) -> Verdict:
    given = reasons(body)
    return Verdict(
        code=sorted({f for f in files if f.startswith(CODE)}),
        docs=sorted({f for f in files if is_doc(f)}),
        reason=next((r for r in given if r), None),
        empty="" in given,
    )


def report(found: Verdict) -> str:
    if not found.code:
        return "No file under src/ changed: the docs need not."
    if found.docs:
        return f"src/ changed, and so did the docs: {', '.join(found.docs)}."
    if found.reason is not None:
        return f"src/ changed without the docs; the body says why: Docs: none ({found.reason})."
    shown = ", ".join(found.code[:10]) + (f" and {len(found.code) - 10} more" if len(found.code) > 10 else "")
    lines = [
        f"src/ changed ({shown}), but nothing under docs/, nor README.md or .env.example.",
        '  Update the docs for what the change adds or changes (AGENTS.md "Documentation"),',
        f"  or say why none needs it in a line of the body of its own: {FORMAT}",
    ]
    if found.empty:
        lines.append("  The body's Docs: none line gives no reason.")
    return "\n".join(lines)


def gh(*args: str) -> str:
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {args[0]} failed: {out.stderr.strip()}")
    return out.stdout


def fetch(repo: str, number: int) -> tuple[list[str], str]:
    """The pull request's changed files (a renamed file by both paths) and its body."""
    listed = gh(
        f"repos/{repo}/pulls/{number}/files",
        "--paginate",
        "--jq",
        ".[] | .filename, (.previous_filename // empty)",
    )
    pull = json.loads(gh(f"repos/{repo}/pulls/{number}"))
    return [line for line in listed.splitlines() if line], str(pull.get("body") or "")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    repo, number = argv[0], int(argv[1])
    try:
        files, body = fetch(repo, number)
    except (RuntimeError, ValueError) as error:
        print(f"::error::{error}")
        return 2
    found = verdict(files, body)
    print(report(found))
    if found.passes:
        return 0
    print(f"::error::src/ changed without the docs: update them, or add a body line {FORMAT}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
