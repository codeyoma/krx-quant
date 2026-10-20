"""Fail a pull request that names an issue after "Refs" but will close it on merge.

GitHub closes an issue when a pull request's body has a closing keyword (close, closes, closed,
fix, fixes, fixed, resolve, resolves, resolved) before the issue's number, even inside backticks
or after "not". PR #340 said "Refs #310" and later "switch this to `Closes #310`"; PR #341 said
"Refs #314" and later "This PR does not close #314". Both merges closed issues that scheduled
jobs still post to.

The check reads the pull request's body and the issues GitHub will close on merge
(`closingIssuesReferences`) from the GraphQL API. It fails when an issue named after "Refs" is
one of them, or when the body has a closing keyword before that issue's number: the second
catches the phrase even if GitHub has not yet updated its list after an edit.

An issue linked in the pull request's Development sidebar is in `closingIssuesReferences` too,
but linking or unlinking one does not start the workflow: GitHub's `pull_request` event has no
such activity type. The check sees a sidebar link only at its next run (a push, a body edit,
reopening), so it enforces the body, not the sidebar. Run the script by hand before merging if
someone may have linked an issue there.

Usage: python3 scripts/check_pr_refs.py <owner/repo> <pr number>   (needs `gh` and GH_TOKEN)

Standard library only, so CI runs it without installing the project.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass

# An issue: `#310`, `owner/repo#310` or the issue's URL.
REF = r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/|(?P<repo>[\w.-]+/[\w.-]+)?#)(?P<number>\d+)\b"
REF_PATTERN = re.compile(REF)
# GitHub's closing keywords; it also reads them with a colon ("Closes: #310").
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*" + REF, re.IGNORECASE)
# "Refs #310", "Ref: #310", "Refs #310, #314", "Refs #310 and #314".
_UNNAMED_REF = re.sub(r"\(\?P<\w+>", "(?:", REF)
REFS = re.compile(
    r"\bRefs?\b\s*:?\s*(?P<list>" + _UNNAMED_REF + r"(?:\s*(?:,|&|\band\b)\s*" + _UNNAMED_REF + r")*)",
    re.IGNORECASE,
)

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      body
      closingIssuesReferences(first: 100) {
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}
"""

Issue = tuple[str, int]  # ("owner/repo" in lower case, number)


@dataclass(frozen=True)
class Mention:
    line: int
    phrase: str
    context: str  # the phrase with some of the line around it


@dataclass(frozen=True)
class Problem:
    issue: Issue
    refs: list[Mention]
    closing: list[Mention]  # closing keywords before the issue's number in the body
    listed: bool  # in closingIssuesReferences


def _issue(match: re.Match[str], repo: str) -> Issue:
    named = match.group("url_repo") or match.group("repo") or repo
    return (named.lower(), int(match.group("number")))


def _name(issue: Issue, repo: str) -> str:
    owner_repo, number = issue
    return f"#{number}" if owner_repo == repo.lower() else f"{owner_repo}#{number}"


def _context(line: str, match: re.Match[str], width: int = 50) -> str:
    start, end = max(0, match.start() - width), min(len(line), match.end() + width)
    return ("…" if start else "") + line[start:end].strip() + ("…" if end < len(line) else "")


def _mentions(body: str, pattern: re.Pattern[str], repo: str) -> dict[Issue, list[Mention]]:
    found: dict[Issue, list[Mention]] = {}
    for lineno, line in enumerate(body.splitlines(), 1):
        for match in pattern.finditer(line):
            span = match.group("list") if "list" in pattern.groupindex else match.group(0)
            for ref in REF_PATTERN.finditer(span):
                mention = Mention(lineno, match.group(0), _context(line, match))
                found.setdefault(_issue(ref, repo), []).append(mention)
    return found


def refs(body: str, repo: str) -> dict[Issue, list[Mention]]:
    """Issues named after "Refs", with where."""
    return _mentions(body, REFS, repo)


def closing_phrases(body: str, repo: str) -> dict[Issue, list[Mention]]:
    """Issues with a closing keyword before their number, with the phrase."""
    return _mentions(body, CLOSING, repo)


def problems(body: str, closing: set[Issue], repo: str) -> list[Problem]:
    """Issues named after "Refs" that merging the pull request would close."""
    phrases = closing_phrases(body, repo)
    found: list[Problem] = []
    for issue, named in sorted(refs(body, repo).items()):
        if issue in closing or issue in phrases:
            found.append(Problem(issue, named, phrases.get(issue, []), issue in closing))
    return found


def report(problem: Problem, repo: str) -> str:
    name = _name(problem.issue, repo)
    lines = [f'{name} is named after "Refs", but merging this pull request closes it.']
    lines += [f'  "Refs" at line {m.line}: {m.phrase!r}' for m in problem.refs]
    lines += [f"  closing phrase at line {m.line}: {m.phrase!r} in {m.context!r}" for m in problem.closing]
    if problem.listed:
        lines.append(f"  GitHub lists {name} in the pull request's closingIssuesReferences.")
        if not problem.closing:
            lines.append(
                "  The body has no closing phrase this check reads: look for the link in the"
                " pull request's Development sidebar."
            )
    lines.append(
        '  Keep the issue open: write the keyword without the number (quote it as "Closes"). '
        'Or, if the pull request should close it, write "Closes" instead of "Refs".'
    )
    return "\n".join(lines)


def fetch(repo: str, number: int) -> tuple[str, set[Issue]]:
    owner, name = repo.split("/", 1)
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}"]
        + ["-F", f"number={number}", "-f", f"query={QUERY}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    pull = data["data"]["repository"]["pullRequest"]
    closing = {
        (node["repository"]["nameWithOwner"].lower(), int(node["number"]))
        for node in pull["closingIssuesReferences"]["nodes"]
    }
    return str(pull["body"] or ""), closing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    repo, number = argv[0], int(argv[1])
    try:
        body, closing = fetch(repo, number)
    except RuntimeError as error:
        print(f"::error::{error}")
        return 2
    found = problems(body, closing, repo)
    for problem in found:
        print(report(problem, repo))
        # One line per issue on the run's summary page.
        phrases = ", ".join(f"{m.phrase!r} at line {m.line}" for m in problem.closing) or "listed by GitHub"
        print(f'::error::{_name(problem.issue, repo)} is named after "Refs" but closes on merge: {phrases}')
    if found:
        return 1
    named = ", ".join(_name(issue, repo) for issue in sorted(refs(body, repo)))
    print(f'Issues named after "Refs" stay open on merge: {named or "none named"}.')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
