# PR checklist

Every change to this repository or the fork goes through these steps. A task handed to an agent points here instead of restating them.

## Worktree

Work in your own worktree; the main checkouts are shared (AGENTS.md "Shared checkouts", #325). `$main` is the main checkout, wherever this machine keeps it; set it from any worktree of the repository:

```bash
main=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
```

- krx-quant: `git -C "$main" fetch origin && git -C "$main" worktree add <scratchpad>/<name> -b <branch> origin/main`
- Fork: `git -C "$main/fincept" fetch origin && git -C "$main/fincept" worktree add <scratchpad>/<name> -b <branch> origin/custom`

After the merge, remove the worktree and delete the branch, locally and on the remote, one branch at a time.

## Checks

- krx-quant runs what CI runs (`.github/workflows/ci.yml`): `uv run python scripts/check_forbidden.py`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest -q`. All pass before the PR is opened.
  - A test past its time limit ends the pytest run with every thread's stack, and the tests after it do not run (CODING_STANDARDS.md, "Tests wait for conditions"). Fix that test and run again.
- Fork: a dev build with `CI=1` (`-Werror`) and `ctest -R krx` (`fincept/custom/README.md`).
  - MSVC treats warnings as errors and macOS clang misses some of them: pass `f` literals to Qt float parameters (`setAlphaF(0.3f)`), and avoid narrowing double → float.
  - Until fork PRs run checks of their own (#324), run the Windows branch check before merging: `gh workflow run custom-release.yml --repo codeyoma/FinceptTerminal --ref <branch>`.

## Commits

- Stage paths explicitly: `git add <path> ...`.
- English subject in the repo's style (`Area: what changed (#issue)`), a body saying why, and the attribution trailer the session's instructions give.

## Docs

- Every PR updates the docs for what it adds or changes (AGENTS.md "Documentation").
- A change note cites the real PR number: open the PR, then put its number in the notes. CI fails on a stand-in (#321).
- Product docs are Korean; agent-facing files (AGENTS.md, `docs/agents/`, `CODING_STANDARDS.md`) are English.

## Pull requests

- Against `main` (krx-quant) or `custom` (fork). Never push to either directly; never enable auto-merge.
- English title and body: what changed and why, tests, what the next 06:00 deploy does, and decisions left for the user. `Closes #N` for the issue it finishes, `Refs #N` for a part of it. A fork PR names `codeyoma/krx-quant#N`.
  - A PR that keeps its issue open never writes a closing keyword (close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved) followed by the issue's number anywhere in the body: GitHub closes the issue on merge even inside backticks or after "not" (#340, #341). Quote it as "Closes" without the number if you must mention it. The `pr-refs` check (`.github/workflows/pr-refs.yml`) fails such a PR and runs again when the body is edited.
- End the body with the attribution line the session's instructions give.
- Both repositories are public: no secrets, `.env` values, server addresses, account numbers or market data, in code, tests, docs, screenshots or PR text. Tests use made-up values.

## Reviews

- Codex reviews each PR. Fix each finding on the PR branch with a test, then answer and resolve its thread in one step: `scripts/reply-resolve.sh <owner/repo> <pr> <comment_id> "<what changed>"`.
- A finding that the evidence contradicts gets a reply with that evidence instead of a change (CODING_STANDARDS.md: probes over fixtures).

## Stacked PRs

When PR B is based on PR A's branch, squash-merge A, then merge `origin/main` into B (never rebase or force-push). Resolve the conflicts with B's side when `main`'s tree equals A's head. Then retarget B to `main` (`gh pr edit <B> --base main`) and merge it once CI passes.
