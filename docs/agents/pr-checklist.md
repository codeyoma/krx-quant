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

- krx-quant runs what CI runs (`.github/workflows/ci.yml`): `uv run python scripts/check_forbidden.py`, `uv run python scripts/check_docs_drift.py`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest -q`. All pass before the PR is opened, but for the PR number stand-ins, which wait for the PR's number: until then run `uv run python scripts/check_forbidden.py --draft` (Docs).
  - A test past its time limit ends the pytest run with every thread's stack, and the tests after it do not run (CODING_STANDARDS.md, "Tests wait for conditions"). Fix that test and run again.
  - CI runs the checks once (job `lint`) and pytest in four shards on runners of their own (`pytest 1` to `pytest 4`, `scripts/ci_shard.py`, #486). The check to wait for is `test`: it passes when `lint` and every shard passed and every test ran in exactly one shard. Locally, run the whole suite in one `pytest`; to run one shard's files, `uv run pytest -q $(python3 scripts/ci_shard.py files <shard> 4)`.
  - A push to a PR cancels that PR's run still going; wait for the newest commit's run.
  - The shards are dealt by `scripts/ci_shard_seconds.json`: each test file's median seconds over the last five green `ci` runs on `main` (their `ci-shard-seconds` artifacts, #590). Refresh it with the command below when a run's shards end more than a minute apart (the `test` job's table) or test files were added or removed. A PR that makes one file much slower or faster also copies that file's seconds from its own run's artifact into the record.
  - `scripts/check_docs_drift.py` (#495) compares three lists with the docs, both ways: the settings `config.py` and `trader/settings.py` read with `.env.example` and docs/operations.md "설정 값 전체"; the commands the family modules of `cli/` add (#578) with README.md's "명령" table; `scheduled.JOBS` with docs/operations.md's "예약 작업 (#215)" table. A name you add goes in each of its docs. A name you remove keeps its place with a 삭제됨 or 바뀜 note that names it (in its section; in `.env.example`, in its block), or its table row begins its second cell with the note. A variable only compose or a deploy script reads goes in the settings section's table with a "읽는 곳" column, naming the files that read it. A setting no doc should list goes in the script's `UNDOCUMENTED` with why. It also fails a table row right after a quote line in README.md or docs/ (Markdown reads it as the quote's text): after a note inside a table, leave a blank line and repeat the table's header row.
- Fork: a dev build with `CI=1` (`-Werror`) and `ctest -R krx` (`fincept/custom/README.md`).
  - MSVC treats warnings as errors and macOS clang misses some of them: pass `f` literals to Qt float parameters (`setAlphaF(0.3f)`), and avoid narrowing double → float.
  - A fork PR runs the Windows build check by itself (AGENTS.md "Submodule workflow", step 7); merge once it passes.

    > **삭제됨** (2026-11-04, #491): "Until fork PRs run checks of their own (#324), run the Windows branch check before merging: `gh workflow run custom-release.yml --repo codeyoma/FinceptTerminal --ref <branch>`." 이유: fork PR이 스스로 검사를 돈다(#324, 2026-10-20 닫힘). 대신: fork PR의 검사가 통과하면 머지한다.

The record's refresh, from the worktree's root. A file no run has (a new one) is left out and counts as the median file; a file deleted since drops out.

```bash
dir=$(mktemp -d)
for run in $(gh run list --workflow ci --branch main --status success --limit 5 --json databaseId --jq '.[].databaseId'); do
  gh run download "$run" --name ci-shard-seconds --dir "$dir/$run"
done
python3 - "$dir"/*/ci_shard_seconds.json > scripts/ci_shard_seconds.json <<'EOF'
import json, statistics, sys
sys.path.insert(0, "scripts")
from ci_shard import ROOT, test_files

runs = [json.load(open(path)) for path in sys.argv[1:]]
seconds = {file: [run[file] for run in runs if file in run] for file in test_files(ROOT)}
print(json.dumps({f: round(statistics.median(s), 2) for f, s in seconds.items() if s}, indent=1, sort_keys=True))
EOF
```

## Commits

- Stage paths explicitly: `git add <path> ...`.
- English subject in the repo's style (`Area: what changed (#issue)`), a body saying why, and the attribution trailer the session's instructions give.

## Docs

- Every PR updates the docs for what it adds or changes (AGENTS.md "Documentation").
- A change note cites the real PR number. Write a stand-in, open the PR as a draft, run `uv run python scripts/fill_pr_number.py <PR>` in the worktree, commit and push, then mark the PR ready (`gh pr ready`). The script replaces what the placeholder rule finds, only in the files the branch changed against `origin/main`, and prints each file and its lines; run again, it changes nothing. A stand-in glued to another repository's name (`codeyoma/FinceptTerminal#…`) is left and listed: fill that in by hand.
- A draft's CI run skips the placeholder rule alone (`check_forbidden.py --draft`, #507) and lists the stand-ins it skipped. The run that marking the PR ready starts, and a push to `main`, fail on one (#321).
  > **바뀜** (2026-11-04, #513): PR을 연 뒤 노트에 번호를 손으로 넣었고, 자리표시는 초안의 CI도 실패시켰다 → 초안으로 열고 `scripts/fill_pr_number.py`로 채운 뒤 리뷰 준비로 바꾼다. 이유: CI 실패 31번 중 22번이 첫 push의 자리표시였다(#507).
- A PR that changes `src/` but nothing under `docs/`, nor `README.md` or `.env.example`, fails the `pr-docs` check (`scripts/check_pr_docs.py`, #507). When no doc needs a change, say why in a line of the body of its own: `Docs: none (<reason>)`. A line with no reason fails; editing the body runs the check again.
- Product docs are Korean; agent-facing files (AGENTS.md, `docs/agents/`, `CODING_STANDARDS.md`) are English.

## Pull requests

- Against `main` (krx-quant) or `custom` (fork). Never push to either directly; never enable auto-merge.
- Open a krx-quant PR as a draft, and mark it ready once its PR numbers are filled in (Docs). Marking it ready runs CI again.
- English title and body: what changed and why, tests, what the next 06:00 deploy does, and decisions left for the user. `Closes #N` for the issue it finishes, `Refs #N` for a part of it. A fork PR names `codeyoma/krx-quant#N`.
  - A PR that keeps its issue open never writes a closing keyword (close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved) followed by the issue's number anywhere in the body: GitHub closes the issue on merge even inside backticks or after "not" (#340, #341). Quote it as "Closes" without the number if you must mention it. The `pr-refs` check (`.github/workflows/pr-refs.yml`) fails such a PR and runs again when the body is edited.
- End the body with the attribution line the session's instructions give.
- Both repositories are public: no secrets, `.env` values, server addresses, account numbers or market data, in code, tests, docs, screenshots or PR text. Tests use made-up values.

## Reviews

- Codex reviews each PR. Fix each finding on the PR branch with a test, then answer and resolve its thread in one step: `scripts/reply-resolve.sh <owner/repo> <pr> <comment_id> "<what changed>"`.
- A finding that the evidence contradicts gets a reply with that evidence instead of a change (CODING_STANDARDS.md: probes over fixtures).

## Stacked PRs

When PR B is based on PR A's branch, squash-merge A, then merge `origin/main` into B (never rebase or force-push). Resolve the conflicts with B's side when `main`'s tree equals A's head. Then retarget B to `main` (`gh pr edit <B> --base main`) and merge it once CI passes.
