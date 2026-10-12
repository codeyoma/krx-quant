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

## Documentation

- Other agents learn the system from `docs/`, `README.md` and `.env.example`, not from chat history. Every PR updates the docs for what it adds or changes: settings, commands, API fields, stored files and keys, schedules, alerts and decisions.
- Nothing is removed silently. When a feature, setting, command, field or decision is removed or replaced, its section keeps a note, in Korean like the rest of the docs:
  - `> **삭제됨** (YYYY-MM-DD, #PR): 무엇이 없어졌는지. 이유: …. 대신: ….`
  - `> **바뀜** (YYYY-MM-DD, #PR): 이전 → 지금. 이유: ….`
- The reason is the one recorded in the PR or the commit. If none was recorded, write "이유 기록 없음".
- Changes committed straight to `main` before PR #31 are cited by commit SHA instead of `#PR`.

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
6. Upstream Fincept releases merge by themselves: the fork's daily `custom-upstream-watch` workflow merges a new release into `custom`, trial-builds it, lands it, and tags the personal build `custom-<tag>-krx.1` (`fincept/custom/README.md`, "upstream 릴리스 반영"). When it opens an "Upstream <tag>: merge needs a hand" issue on the fork, finish inside `fincept/` with `custom/merge-upstream-release.sh` as the issue says. The `fincept` pointer here moves past the merge with the next krx-quant commit that touches `fincept/`.
   > **바뀜** (2026-10-12, #94): upstream 릴리스는 `fincept/` 안에서 손으로 merge 하고 포인터를 올렸다 → fork workflow가 merge·시험 빌드·`custom` 반영·태그까지 한다. 이유: upstream이 릴리스를 내면 업데이트가 저절로 돌게 하려고(#93).
