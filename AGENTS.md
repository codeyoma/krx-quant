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
- `#PR` is the real number: open the PR, then put its number in the notes. CI's forbidden-pattern check fails while a stand-in for it is left (#321).
- Changes committed straight to `main` before PR #31 are cited by commit SHA instead of `#PR`.

## Timed server work

- Work that must run on the server on a given day or at a given time (a diagnosis after a deploy, a pre-validation once enough days are recorded, a measurement a decision waits for) is a scheduled job: add an entry to `JOBS` in `src/krx_quant/scheduled.py` in a PR, with its condition, command, time limit, issues and labels. Do not open a `ready-for-human` issue asking the user to remember a date and run a command.
- The server runs it at night, after the nightly training, and posts the result as a comment on the job's issues, which then get `ready-for-agent`. Read it with `gh issue view <n> --comments`. See `docs/operations.md` "예약 작업 (#215)".
- A new algorithm needs no entry: its pre-validation job is built from the registries (`prevalidation_jobs`) and posts to `PREVALIDATION_ISSUE` (#204). Once #204 is closed, point that at an open issue in the algorithm's PR.

## Repository layout

- Engine code, algorithms and docs live in this repo. It is public: never commit secrets, `.env`, recorded market data or server addresses.
  > **바뀜** (2026-10-14, #194): 이 저장소는 비공개였다 → 공개. 이유: 비공개 저장소의 GitHub Actions 시간이 바닥났고(공개 저장소는 무료다), 알고리즘과 모델은 보여도 된다고 정했다. 공개 전에 git 이력, 이슈, PR에 비밀값, 데이터 파일, 서버 주소가 없는 것을 확인했다.
- `fincept/` is a git submodule: the public fork `codeyoma/FinceptTerminal`, tracking its `custom` branch. Only Fincept screen code goes there: engine code and algorithm details stay in this repo, so each lives in one place. Secrets go in neither; both are public.
  > **바뀜** (2026-10-14, #194): "`fincept/`는 공개라 엔진 코드, 알고리즘 설명, 비밀값을 두지 않는다" → 엔진 코드는 한 곳에 두려고 이 저장소에 두고, 비밀값은 두 저장소 어디에도 두지 않는다. 이유: 이 저장소도 공개가 됐다.
- The fork has its own rules and agent docs: `fincept/custom/README.md`, `fincept/custom/AGENTS.md`.
- CI for this repo does not need the submodule; check out without submodules unless a job builds the screen.

## Submodule workflow

1. Fresh clone: `git clone --recurse-submodules https://github.com/codeyoma/krx-quant.git`. Existing clone: `git submodule update --init`. Then set `git config push.recurseSubmodules check` once per clone.
2. Before editing in `fincept/`, run `git -C fincept switch custom && git -C fincept pull --ff-only`. A fresh submodule checkout is a detached HEAD.
3. Commit inside `fincept/` and push it to `origin custom` first.
4. Do not commit the `fincept` pointer. It follows `custom`'s head by itself: after every push to `custom` the fork's `custom-krx-pointer` workflow sends a `repository_dispatch`, and this repo's `fincept-pointer` workflow (`.github/workflows/fincept-pointer.yml`) pushes the pointer move to `main`; a daily run (05:30 KST) and a manual run catch a missed one. A screen change needs no krx-quant PR unless docs change. When a screen change needs an engine change (for example an Engine API change), merge the engine PR first: the fork merge reaches the app as a personal build at once, while the engine reaches the server at the next 06:00 update.
   > **바뀜** (2026-10-14, #188): 화면 PR마다 krx-quant에서 `git add fincept`로 포인터를 올리고, 화면이 기대는 엔진 변경과 한 커밋에 묶었다 → 포인터는 workflow가 `custom`의 최신 커밋으로 옮기고 PR은 포인터를 건드리지 않는다. 이유: 화면 PR 둘이 동시에 열리면 포인터에서 충돌하고, fork PR을 squash 머지한 뒤 포인터를 다시 옮겨야 했다. krx-quant의 CI·배포·엔진은 `fincept/`를 읽지 않는다. 사용자가 늘 `custom` 최신을 가리키기로 정했다(#186).
5. Never push a krx-quant commit whose `fincept` pointer is not on the fork's remote. `push.recurseSubmodules=check` enforces this.
6. Upstream Fincept releases merge and build by themselves: the fork's daily `custom-upstream-watch` workflow merges a new release into `custom`, trial-builds it, lands it, and tags the personal build `custom-<tag>-krx.1` (`fincept/custom/README.md`, "upstream 릴리스 반영"). Every later push to `custom` that changes the app is tagged and built by `custom-auto-tag`. When the watch opens an `Upstream <tag>: merge needs a hand` issue on the fork, finish inside `fincept/` with `custom/merge-upstream-release.sh` as the issue says. The `fincept` pointer here follows the landing by itself (step 4).
   > **바뀜** (2026-10-12, #94): upstream 릴리스는 `fincept/` 안에서 손으로 merge 하고 포인터를 올렸다 → fork workflow가 merge·시험 빌드·`custom` 반영·태그까지 한다. 이유: upstream이 릴리스를 내면 업데이트가 저절로 돌게 하려고(#93).
   > **바뀜** (2026-10-14, #194, fork codeyoma/FinceptTerminal#46): workflow가 merge 뒤 시험 빌드를 바로 시작했고, `custom`에 push 될 때마다 개인 빌드가 나왔다 → 시험 빌드와 개인 빌드 모두 손으로 시작한다. 이유: 업데이트마다 맥·윈도우 빌드가 돌았고, 빌드는 필요할 때만 있으면 된다(2026-10-14 결정).
   > **바뀜** (2026-10-15, #202, fork codeyoma/FinceptTerminal#51): 시험 빌드와 개인 빌드를 손으로 시작했다 → 다시 저절로 돈다. 이유: fork는 공개 저장소라 Actions가 무료이고, 한도를 쓴 것은 비공개였던 krx-quant의 CI였다(지금은 공개).
   > **바뀜** (2026-10-14, #188): 포인터는 `fincept/`를 건드리는 다음 krx-quant 커밋이 올렸다 → `fincept-pointer` workflow가 바로 올린다. 이유: 위 4번과 같다(#186).
