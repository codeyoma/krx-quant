## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `codeyoma/krx-quant` (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`, plus the agreed system design in `docs/design.md`. See `docs/agents/domain.md`.

## Git workflow

- Never push to `main` directly. Start each change on a branch from `origin/main`, push the branch, open a pull request against `main` (`Closes #N` for the issue it implements), and merge it from the PR once CI passes.
- The one exception is the `fincept-pointer` workflow: it pushes commits that change only the `fincept` pointer straight to `main` (Submodule workflow, step 4).
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
4. Do not commit the `fincept` pointer. It follows `custom`'s head by itself: after every push to `custom` the fork's `custom-krx-pointer` workflow sends a `repository_dispatch`, and this repo's `fincept-pointer` workflow (`.github/workflows/fincept-pointer.yml`) pushes the pointer move to `main`; a daily run (05:30 KST) and a manual run catch a missed one. A screen change needs no krx-quant PR unless docs change. When a screen change needs an engine change (for example an Engine API change), merge the engine PR first: the fork merge reaches the app as a personal build at once, while the engine reaches the server at the next 06:00 update.
   > **바뀜** (2026-10-14, #PRNUM): 화면 PR마다 krx-quant에서 `git add fincept`로 포인터를 올리고, 화면이 기대는 엔진 변경과 한 커밋에 묶었다 → 포인터는 workflow가 `custom`의 최신 커밋으로 옮기고 PR은 포인터를 건드리지 않는다. 이유: 화면 PR 둘이 동시에 열리면 포인터에서 충돌하고, fork PR을 squash 머지한 뒤 포인터를 다시 옮겨야 했다. krx-quant의 CI·배포·엔진은 `fincept/`를 읽지 않는다. 사용자가 늘 `custom` 최신을 가리키기로 정했다(#186).
5. Never push a krx-quant commit whose `fincept` pointer is not on the fork's remote. `push.recurseSubmodules=check` enforces this.
6. Upstream Fincept releases merge by themselves: the fork's daily `custom-upstream-watch` workflow merges a new release into `custom`, trial-builds it, lands it, and tags the personal build `custom-<tag>-krx.1` (`fincept/custom/README.md`, "upstream 릴리스 반영"). When it opens an `Upstream <tag>: merge needs a hand` issue on the fork, finish inside `fincept/` with `custom/merge-upstream-release.sh` as the issue says. The `fincept` pointer here follows the landing by itself (step 4).
   > **바뀜** (2026-10-12, #94): upstream 릴리스는 `fincept/` 안에서 손으로 merge 하고 포인터를 올렸다 → fork workflow가 merge·시험 빌드·`custom` 반영·태그까지 한다. 이유: upstream이 릴리스를 내면 업데이트가 저절로 돌게 하려고(#93).
   > **바뀜** (2026-10-14, #PRNUM): 포인터는 `fincept/`를 건드리는 다음 krx-quant 커밋이 올렸다 → `fincept-pointer` workflow가 바로 올린다. 이유: 위 4번과 같다(#186).
