## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `codeyoma/krx-quant` (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`, plus the agreed system design in `docs/design.md`. See `docs/agents/domain.md`.

## Git workflow

- Never push to `main` directly. Start each change on a branch from `origin/main`, push the branch, open a pull request against `main` (`Closes #N` for the issue it implements), and merge it from the PR once CI passes.
- The `fincept/` fork follows its own rules (`fincept/custom/AGENTS.md`).

## Repository layout

- Engine code, algorithms and docs live in this private repo.
- `fincept/` is a git submodule: the public fork `codeyoma/FinceptTerminal`, tracking its `custom` branch. Only Fincept screen code goes there. Never put engine code, algorithm details or secrets in `fincept/`; it is public.
- The fork has its own rules and agent docs: `fincept/custom/README.md`, `fincept/custom/AGENTS.md`.
- CI for this repo does not need the submodule; check out without submodules unless a job builds the screen.

## Submodule workflow

1. Fresh clone: `git clone --recurse-submodules https://github.com/codeyoma/krx-quant.git`. Existing clone: `git submodule update --init`. Then set `git config push.recurseSubmodules check` once per clone.
2. Before editing in `fincept/`, run `git -C fincept switch custom && git -C fincept pull --ff-only`. A fresh submodule checkout is a detached HEAD.
3. Commit inside `fincept/` and push it to `origin custom` first.
4. Then, in this repo, `git add fincept` and commit the pointer bump. Put it in the same commit as any engine change the screen depends on (for example an Engine API change), so engine and screen versions move together.
5. Never push a krx-quant commit whose `fincept` pointer is not on the fork's remote. `push.recurseSubmodules=check` enforces this.
6. Upstream Fincept releases are merged inside `fincept/` with `custom/merge-upstream-release.sh`; then commit the pointer bump here.
