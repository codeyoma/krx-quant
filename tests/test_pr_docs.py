"""The pull request check (``scripts/check_pr_docs.py``, #507): a change to ``src/`` comes with
its docs, or the body says why not.

Codex's reviews had flagged eight PRs whose docs had not followed the code (AGENTS.md
"Documentation").
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

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_pr_docs.py"
REPO = "codeyoma/krx-quant"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pr_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


check = load_check()

CODE = ["src/krx_quant/collector.py", "tests/test_collector.py"]


def test_a_change_to_src_alone_fails() -> None:
    verdict = check.verdict(CODE, "Closes #1\n\nWhat changed and why.\n")
    assert not verdict.passes
    assert verdict.code == ["src/krx_quant/collector.py"]
    assert "src/krx_quant/collector.py" in check.report(verdict)
    assert "Docs: none (<reason>)" in check.report(verdict)


DOCS = ["docs/operations.md", "docs/agents/pr-checklist.md", "README.md", ".env.example"]


@pytest.mark.parametrize("doc", DOCS)
def test_a_change_to_src_with_any_of_the_docs_passes(doc: str) -> None:
    verdict = check.verdict([*CODE, doc], "")
    assert verdict.passes and verdict.docs == [doc]


@pytest.mark.parametrize(
    "files",
    [
        ["tests/test_collector.py"],
        ["scripts/ci_shard.py", ".github/workflows/ci.yml"],
        ["AGENTS.md"],
        [],
    ],
)
def test_a_pull_request_that_leaves_src_alone_needs_no_docs(files: list[str]) -> None:
    assert check.verdict(files, "").passes


@pytest.mark.parametrize(
    "files",
    [
        ["fincept/custom/README.md", *CODE],  # another README than the repository's
        ["research/docs/notes.md", *CODE],  # a docs/ inside another directory
        ["docs.md", *CODE],
    ],
)
def test_only_the_repository_s_docs_count(files: list[str]) -> None:
    assert not check.verdict(files, "").passes


@pytest.mark.parametrize(
    "line",
    [
        "Docs: none (a refactor: no setting, command or field changes)",
        "docs: None (tests only (#486))",
        "  Docs: none (the log line's wording only)  ",
    ],
)
def test_a_docs_none_line_with_a_reason_passes(line: str) -> None:
    verdict = check.verdict(CODE, f"Closes #1\n\n{line}\n\nGenerated with a tool.\n")
    assert verdict.passes and verdict.reason
    assert verdict.reason in check.report(verdict)


@pytest.mark.parametrize(
    "line",
    [
        "Docs: none ()",
        "Docs: none (   )",
        "Docs: none (<reason>)",  # the format, copied as it is
        "Docs: none (<이유>)",
        "Docs: none",
        "Docs: none because it is a refactor",
        "We write `Docs: none (a reason)` when no doc changes.",  # inside a sentence: prose
    ],
)
def test_a_docs_none_line_without_a_reason_fails(line: str) -> None:
    verdict = check.verdict(CODE, f"Closes #1\n\n{line}\n")
    assert not verdict.passes


def test_an_empty_reason_is_named_in_the_report() -> None:
    verdict = check.verdict(CODE, "Docs: none ()\n")
    assert "gives no reason" in check.report(verdict)


def fake_gh(tmp_path: Path, body: str | None, files: list[dict[str, str]]) -> dict[str, str]:
    """A `gh` on PATH that answers the pull request's and its files' REST calls."""
    (tmp_path / "pull.json").write_text(json.dumps({"number": 507, "body": body}), encoding="utf-8")
    names = [name for f in files for name in (f["filename"], f.get("previous_filename")) if name]
    (tmp_path / "files.txt").write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    (tmp_path / "args.txt").write_text("", encoding="utf-8")
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path / "args.txt"}"\n'
        'case "$*" in\n'
        f'  *pulls/507/files*) cat "{tmp_path / "files.txt"}" ;;\n'
        f'  *pulls/507*) cat "{tmp_path / "pull.json"}" ;;\n'
        "  *) echo unexpected >&2; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), REPO, "507"], capture_output=True, text=True, env=env, timeout=20
    )


def test_the_check_names_the_code_and_passes_once_the_body_says_why(tmp_path: Path) -> None:
    files = [{"filename": "src/krx_quant/collector.py"}, {"filename": "tests/test_collector.py"}]
    failed = run(fake_gh(tmp_path, "Closes #1\n", files))
    assert failed.returncode == 1
    assert "::error::" in failed.stdout and "src/krx_quant/collector.py" in failed.stdout
    # Every page of the files, both of a renamed file's paths.
    args = (tmp_path / "args.txt").read_text()
    assert "--paginate" in args and "previous_filename" in args

    passed = run(fake_gh(tmp_path, "Closes #1\n\nDocs: none (wording of a log line)\n", files))
    assert passed.returncode == 0, passed.stdout
    assert "wording of a log line" in passed.stdout


def test_the_check_reads_a_renamed_doc_s_old_path(tmp_path: Path) -> None:
    files = [
        {"filename": "src/krx_quant/collector.py"},
        {"filename": "notes/feeds.md", "previous_filename": "docs/feeds.md"},
    ]
    passed = run(fake_gh(tmp_path, None, files))  # a pull request without a body
    assert passed.returncode == 0, passed.stdout
    assert "docs/feeds.md" in passed.stdout


def test_the_check_fails_when_github_cannot_be_read(tmp_path: Path) -> None:
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    failed = run({**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"})
    assert failed.returncode == 2
    assert "Bad credentials" in failed.stdout


def test_the_workflow_runs_again_on_a_body_edit_and_on_ready_for_review() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pr-docs.yml").read_text()
    assert "types: [opened, edited, synchronize, reopened, ready_for_review]" in workflow
    assert 'run: python3 scripts/check_pr_docs.py "$REPO" "$PR"' in workflow
    assert "GH_TOKEN: ${{ github.token }}" in workflow
    assert "group: pr-docs-${{ github.event.pull_request.number }}" in workflow
    # Not in ci.yml, where a body edit would run every test shard again.
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "check_pr_docs" not in ci
    assert "types: [opened, synchronize, reopened, ready_for_review]" in ci
