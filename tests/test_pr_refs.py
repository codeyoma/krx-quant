"""The pull request check (``scripts/check_pr_refs.py``): an issue named after "Refs" stays open.

The bodies of PR #340 and #341 named their issue after "Refs" and then wrote a closing keyword
before its number, so merging them closed #310 and #314 (2026-10-21).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_refs.py"
REPO = "codeyoma/krx-quant"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pr_refs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


check = load_check()

# The lines of PR #340's and #341's bodies that closed their issues.
BODY_340 = (
    "Refs #310. The fourth of four stacked PRs. It is based on #339.\n"
    "\n"
    "Close it with the last follow-up, or tell me to switch this to `Closes #310`.\n"
)
BODY_341 = (
    "Refs #314. The screen half is codeyoma/FinceptTerminal#101.\n"
    "\n"
    "This PR does not close #314. One figure is re-measured by a scheduled job that posts to #314.\n"
)


def issue(number: int, repo: str = REPO) -> tuple[str, int]:
    return (repo.lower(), number)


@pytest.mark.parametrize(
    ("body", "number", "phrase"),
    [(BODY_340, 310, "Closes #310"), (BODY_341, 314, "close #314")],
)
def test_the_two_bodies_that_closed_their_issues_fail_even_before_github_lists_the_issue(
    body: str, number: int, phrase: str
) -> None:
    for closing in (set(), {issue(number)}):
        [problem] = check.problems(body, closing, REPO)
        assert problem.issue == issue(number)
        assert [m.phrase for m in problem.closing] == [phrase]
        assert problem.listed == bool(closing)


@pytest.mark.parametrize(
    "phrase",
    [
        "closes #310",
        "Fixed #310",
        "RESOLVES #310",
        "close: #310",
        "fix codeyoma/krx-quant#310",
        "resolved https://github.com/codeyoma/krx-quant/issues/310",
        "`Closes #310`",
    ],
)
def test_every_closing_keyword_form_is_found(phrase: str) -> None:
    [problem] = check.problems(f"Refs #310\n\nDo not write {phrase} here.\n", set(), REPO)
    assert problem.issue == issue(310)


@pytest.mark.parametrize(
    "body",
    [
        'Refs #310. Mention it as "Closes" without the number.',  # the checklist's way
        "Refs #310. The close side is not a measure.",  # the word, not before a number
        "Refs #310. Closes #311.",  # another issue closes
        "Refs #310. Closes codeyoma/FinceptTerminal#310.",  # the same number in another repository
        "Refs #310. It is based on #339, which fixed the overlap.",  # a number before the keyword
        "Closes #310.",  # no "Refs": the pull request means to close it
    ],
)
def test_a_body_that_keeps_its_refs_issue_open_passes(body: str) -> None:
    assert check.problems(body, {issue(311)}, REPO) == []


def test_an_issue_github_lists_without_a_phrase_in_the_body_fails() -> None:
    """A link added in the pull request's Development sidebar closes the issue too."""
    [problem] = check.problems("Refs #310.", {issue(310)}, REPO)
    assert problem.closing == [] and problem.listed
    assert "Development sidebar" in check.report(problem, REPO)


def test_every_issue_in_a_refs_list_is_checked() -> None:
    body = "Refs #310, #314 and codeyoma/FinceptTerminal#101.\n\nThis does not close #314.\n"
    assert set(check.refs(body, REPO)) == {
        issue(310),
        issue(314),
        issue(101, "codeyoma/FinceptTerminal"),
    }
    [problem] = check.problems(body, set(), REPO)
    assert problem.issue == issue(314)


def test_a_cross_repository_refs_issue_that_closes_fails() -> None:
    body = "Refs codeyoma/FinceptTerminal#101."
    [problem] = check.problems(body, {issue(101, "codeyoma/FinceptTerminal")}, REPO)
    assert "codeyoma/finceptterminal#101 is named" in check.report(problem, REPO)


def fake_gh(tmp_path: Path, body: str, closing: list[int]) -> dict[str, str]:
    """A `gh` on PATH that answers the GraphQL query with this pull request."""
    answer = {
        "data": {
            "repository": {
                "pullRequest": {
                    "body": body,
                    "closingIssuesReferences": {
                        "nodes": [{"number": n, "repository": {"nameWithOwner": REPO}} for n in closing]
                    },
                }
            }
        }
    }
    (tmp_path / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    gh = tmp_path / "gh"
    gh.write_text(f'#!/bin/sh\ncat "{tmp_path / "answer.json"}"\n', encoding="utf-8")
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), REPO, "341"], capture_output=True, text=True, env=env, timeout=20
    )


def test_the_check_names_the_issue_and_the_phrase_and_passes_once_the_body_is_fixed(tmp_path: Path) -> None:
    failed = run(fake_gh(tmp_path, BODY_341, [314]))
    assert failed.returncode == 1
    assert '#314 is named after "Refs"' in failed.stdout
    assert "'close #314' at line 3" in failed.stdout

    fixed = BODY_341.replace("does not close #314", 'does not "Close" it')
    passed = run(fake_gh(tmp_path, fixed, []))
    assert passed.returncode == 0, passed.stdout
    assert 'Issues named after "Refs" stay open on merge: #314.' in passed.stdout


def test_the_check_fails_when_github_cannot_be_read(tmp_path: Path) -> None:
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    failed = run({**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"})
    assert failed.returncode == 2
    assert "Bad credentials" in failed.stdout
