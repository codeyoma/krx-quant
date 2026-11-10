# krx-quant

KRX 종목의 4분 뒤 가격 움직임을 여러 퀀트 알고리즘이 예측하고, 예측을 자동 채점해 통계로 보여주는 개인용 엔진이다. 결과는 fork한 Fincept Terminal([codeyoma/FinceptTerminal](https://github.com/codeyoma/FinceptTerminal), `custom` 브랜치)의 화면에서 본다.

엔진(수집기)에는 주문 코드가 없다. 신호만 낸다. 자동 매매(#393)는 따로 도는 trader 프로세스(`krx-quant trade`, compose 서비스 `trader`)가 한다. 모드는 셋이다(서버 `.env`의 `KRX_TRADER_MODE`). 기본값 `paper`(모의)는 수집기가 내보내는 실시간 호가로 체결을 흉내 내고 주문은 내지 않는다. `mock`(KIS 모의, #405)은 챔피언 정책의 매매를 trader 자신의 모의 키로 KIS 모의투자 서버에 실제로 주문하고, 장부를 KIS의 체결로 쓴다. `live`(실전, #408)는 실전 관문과 실행 점검을 넘은 챔피언만 새 실전 계좌의 키(`KRX_TRADER_LIVE_KIS_*`)로 주문하고, 그 전에는 주문 없이 모의로 돌며 모자란 것을 알린다(설계 [14장](docs/design.md#14-자동-매매-393), 모드 바꾸기는 [operations.md](docs/operations.md) "모드 바꾸기 (#405)", "실전 (#408)"). 에이전트는 실전 주문을 내지 않는다: live는 사용자가 서버에서 켠다. KIS 주문 코드는 trader의 KIS 주문 모듈(`src/krx_quant/kis_orders.py`) 하나에만 있고, 그 밖에서는 `scripts/check_forbidden.py`가 계속 막는다. 그것을 쓰는 것은 KIS 모의투자 서버에 주문해 응답을 재는 프로브(`krx-quant probe-kis-orders`, #403), mock과 live 모드의 trader(`kis_orders.Broker`, #404, #405, #408), mock 첫날의 장부를 KIS 체결 조회와 견주는 명령(`krx-quant check-mock-trades`, #405)이다. **바뀜** (2026-11-04, #506): "견주는 예약 작업" → 명령. 그 예약 작업(`check-mock-trades-405`)은 2026-11-02 밤 `같다`로 끝나 `JOBS`에서 빠졌다. 이유: 할 일을 마쳤다.

> **바뀜** (2026-10-31, #425): "모드는 둘이다", "실전(`live`, #408)은 아직 없다" → 모드는 셋이고 live는 관문과 실행 점검을 넘은 챔피언만 실전 계좌에 주문한다. 이유: #408.

> **바뀜** (2026-10-30, #422): "지금은 모의(`paper`)뿐이다", "trader의 주문 모듈은 그 파일에 있지만 trader는 아직 쓰지 않는다(mock 모드, #405)" → `mock` 모드가 KIS 모의투자 서버에 실제로 주문한다. 이유: #405(spec #393의 "세 모드": 실전 전에 KIS 모의투자로 주문과 체결을 확인한다).

> **바뀜** (2026-10-29, #409): "주문 기능은 없다. 신호만 낸다." → 엔진에는 주문 코드가 없고, 자동 매매는 따로 도는 trader가 한다(지금은 모의뿐). 이유: 사용자가 엔진의 예측으로 삼성전자를 스스로 사고파는 자동 매매를 원했다(#393). 주문 코드는 trader의 KIS 주문 모듈에만 둔다(#404에서). 이 PR은 모의뿐이다.

> **바뀜** (2026-10-29, #410): "KIS 주문 코드는 아직 어디에도 없고, `scripts/check_forbidden.py`가 계속 막는다" → `src/krx_quant/kis_orders.py` 하나에만 있고, 그 밖에서는 계속 막는다. 지금은 모의투자 서버의 주문 프로브만 쓴다. 이유: 주문 응답의 필드를 쓰기 전에 모의투자 서버에서 잰다(#403, CODING_STANDARDS "External data is probed before it is used").

## 받기

```bash
git clone --recurse-submodules https://github.com/codeyoma/krx-quant.git
```

`fincept/`는 공개 fork `codeyoma/FinceptTerminal`의 `custom` 브랜치를 가리키는 submodule이다. 작업 순서는 [AGENTS.md](AGENTS.md)의 Submodule workflow를 따른다.

## 개발 환경

[uv](https://docs.astral.sh/uv/)로 Python 3.11 환경을 만든다. 맥에서는 Turing의 LightGBM이 OpenMP를 찾으므로 `brew install libomp`를 한 번 해 둔다(리눅스와 서버 이미지는 `libgomp1`).

```bash
uv sync
```

검사와 테스트:

```bash
uv run python scripts/check_forbidden.py && uv run python scripts/check_docs_drift.py && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
```

테스트 하나가 시간 한도(`pyproject.toml`의 `timeout`, 30초)를 넘으면 `pytest`는 모든 스레드의 스택을 출력하고 그 자리에서 실행 전체를 끝낸다(`timeout_method = "thread"`, #335). 그 뒤의 테스트는 돌지 않는다. 더 오래 걸려야 하는 테스트는 `@pytest.mark.timeout(N)`과 이유를 단다.

CI(`.github/workflows/ci.yml`, #486)는 위의 검사를 job `lint`에서 한 번 돌리고, pytest는 shard 4개(`pytest 1`–`pytest 4`)로 나눠 러너 4개에서 동시에 돌린다. 한 shard 안에서는 테스트가 하나씩 차례로 돈다. 한 러너에서 pytest-xdist로 나누지 않는 것은 테스트끼리 CPU를 다투지 않게 하려는 것이다(실제 경과 시간을 재는 테스트가 있다). `scripts/ci_shard.py`가 테스트 파일을 지난 기록의 초(`scripts/ci_shard_seconds.json`)로 나눈다: 긴 파일부터, 지금까지 초가 가장 적은 shard에 넣는다. 기록에 없는 파일은 기록된 파일의 중앙값으로 친다. 같은 커밋은 늘 같게 나뉜다. 머지 전에 보는 검사는 `test` 하나다: `lint`와 모든 shard가 통과했고, `lint`가 모은 테스트 모두가 정확히 한 shard에서 돌았을 때(빠진 것도 겹친 것도 없을 때) 통과한다. shard 하나의 파일만 돌리려면 `uv run pytest -q $(python3 scripts/ci_shard.py files <shard> 4)`. shard마다 걸린 초는 `test` job의 요약에 나온다. 기록(`scripts/ci_shard_seconds.json`)은 최근 `main`에서 통과한 실행 다섯 번의 `ci-shard-seconds` artifact에서 파일마다 낸 중앙값이다. 한 실행의 shard끼리 1분 넘게 벌어지거나 테스트 파일이 늘거나 줄면 다시 만들고, 파일 하나의 시간을 크게 바꾸는 PR은 그 파일의 초를 자기 실행의 artifact에서 가져온다(명령은 `docs/agents/pr-checklist.md`의 "Checks", #590). shard는 30분, `lint`는 10분을 넘으면 멈춘다(`timeout-minutes`, 평소 shard 6–8분, `lint` 1분, 실행 전체 약 8분). PR에서는 열 때, push 할 때, 다시 열 때, 초안을 리뷰 준비로 바꿀 때(`ready_for_review`, #513) 돈다. 본문을 고칠 때는 돌지 않는다: 그때마다 shard 4개가 다시 돌 것이다. PR에 새 커밋을 push 하거나 초안을 리뷰 준비로 바꾸면 그 PR의 아직 도는 실행은 취소된다. `main` push의 실행은 취소되지 않는다.

> **바뀜** (2026-11-06, #597): shard끼리 시간이 벌어지면 최근 `main` 실행 한 번의 `ci-shard-seconds` artifact로 기록을 덮어썼다 → 최근 통과한 실행 다섯 번의 파일별 중앙값으로 다시 만든다. 이유: 같은 파일의 초가 실행마다 크게 달랐고(11-06 새벽 `main`의 통과한 실행 다섯 번에서 `tests/test_nightly.py`가 62–121초), 기록에 테스트 파일 207개 중 25개가 빠져 있었다(#590).

> **바뀜** (2026-11-04, #491): CI의 job 하나가 검사와 pytest 전체를 한 러너에서 차례로 돌렸고 60분을 넘으면 멈췄다(평소 11–22분) → `lint` 하나와 pytest shard 4개가 동시에 돌고, 모음 검사 `test`가 모두를 기다린다. PR의 지난 실행은 새 push가 취소한다. 이유: 성공한 실행의 중앙값이 2026-10-19 15.8분에서 11-03 26.9분이 되어(pytest 1,549초) 약 7주 뒤 60분 제한에 닿을 참이었고, 10-19 이후 PR 실행 294번 중 102번이 같은 브랜치의 새 실행이 시작된 뒤에도 돌고 있었다(#486).

`scripts/check_forbidden.py`는 KIS 주문 TR 코드와 주문 엔드포인트(trader의 KIS 주문 모듈 `src/krx_quant/kis_orders.py` 밖에서, `forbidden.ALLOWED_IN`), 비밀값 패턴(`RULES`)과 바뀜·삭제됨 노트에 남은 PR 번호 자리표시(`REPOSITORY_RULES`, #321)가 저장소에 들어오면 실패한다. CI도 같은 검사를 한다. 패턴은 `src/krx_quant/forbidden.py`에 있다. 예약 작업이 GitHub 이슈에 남기는 댓글에서는 `RULES`만 가린다(#215).

> **바뀜** (2026-10-29, #410): KIS 주문 TR 코드와 주문 엔드포인트는 어느 파일에서도 실패했다 → `src/krx_quant/kis_orders.py` 하나에서만 풀린다(그 파일에서도 다른 규칙은 그대로, 토스 주문 규칙도 그대로). 게시물 가림(`redaction.py`)은 그 파일의 TR 코드도 가린다. 이유: 자동 매매(#393)의 주문 코드를 한 곳에 두고, 다른 파일에 주문 코드가 새면 계속 막으려고(#403).

> **바뀜** (2026-10-20, #328): 검사는 주문 코드와 비밀값만 봤다 → PR 번호 자리표시도 본다(저장소 검사에만). 이유: PR을 열기 전에 쓴 자리표시가 남은 채 리뷰에 온 PR이 일곱 개였다(#321).

> **바뀜** (2026-10-24, #375): 자리표시는 `#` 뒤의 `PR`로 시작하는 대문자 이름, `NNN`, `XXX`, `TBD`, 밑줄로 감싼 `PR`이었다 → 대문자로 시작해 `PR`로 끝나는 이름도 자리표시다(`src/krx_quant/forbidden.py`의 주석에 예가 있다). 이유: #375가 그런 자리표시를 단 채 리뷰에 왔고 검사가 잡지 못했다(Codex 리뷰).

초안 PR의 CI는 `--draft`를 붙여 PR 번호 자리표시 규칙 하나만 건너뛴다(#507): 노트에 적을 PR 번호는 PR을 연 뒤에야 안다. 건너뛴 자리표시도 줄마다 출력하고 경고로 남긴다. 비밀값, 주문 코드 같은 다른 규칙은 초안에서도 그대로 실패한다. 초안을 리뷰 준비로 바꾸면(`gh pr ready`) CI가 다시 돌며 자리표시를 본다. `main` push와 `--draft` 없이 돌린 검사도 늘 본다. PR을 열기 전 로컬에서는 `uv run python scripts/check_forbidden.py --draft`로 돌린다.

자리표시는 `uv run python scripts/fill_pr_number.py <PR 번호>`가 채운다(#507). 그 브랜치가 `origin/main`에 대해 바꾼 파일(커밋했든 안 했든. 추적하지 않는 파일과 검사가 건너뛰는 두 파일은 뺀다)에서 자리표시 규칙이 찾는 것을 `#<PR 번호>`로 바꾸고, 바꾼 파일과 줄 번호를 출력한다. 패턴은 검사와 같은 것(`src/krx_quant/forbidden.py`)이다. 다른 저장소 이름에 붙은 자리표시(`codeyoma/FinceptTerminal#…`)는 그 저장소의 PR이라 그대로 두고 따로 출력한다. 다시 돌려도 더 바꾸지 않는다.

> **바뀜** (2026-11-04, #513): PR 번호 자리표시는 초안 PR에서도 CI를 실패시켰고, 번호는 손으로 넣었다 → 초안에서는 자리표시 규칙만 건너뛰고, `scripts/fill_pr_number.py`가 번호를 채우며, 리뷰 준비로 바꿀 때 CI가 다시 돌아 자리표시를 본다(`ci.yml`이 `ready_for_review`에도 돈다). 이유: CI 실패 31번 중 22번이 첫 push의 자리표시였다. 에이전트는 초안 PR을 연 뒤에 번호를 채우는데, #486 뒤로는 push마다 lint와 pytest shard 4개가 돈다(#507).

`scripts/check_docs_drift.py`(#495)는 코드와 문서의 목록 셋을 양쪽으로 견주어, 한쪽에만 있는 이름이 있으면 실패한다: 엔진이 읽는 설정(`src/krx_quant/config.py`, `src/krx_quant/trader/settings.py`)과 `.env.example`, [docs/operations.md](docs/operations.md#설정-값-전체)의 "설정 값 전체" 표, `src/krx_quant/cli/`의 명령 묶음 모듈(#578)이 더하는 명령과 아래 "명령" 표, 예약 작업 `JOBS`와 docs/operations.md의 "예약 작업 (#215)" 표다. 문서에만 남은 이름은 그 자리(같은 절, `.env.example`에서는 빈 줄 없이 이어진 덩어리)에 그 이름을 적은 삭제됨·바뀜 노트가 있거나, 표의 그 줄이 노트로 시작하면(`| 이름 | **삭제됨** (…): … |`) 통과한다. 엔진은 읽지 않고 compose와 스크립트만 읽는 변수는 "설정 값 전체"의 "읽는 곳" 표에 두고, 그 줄이 적은 파일마다 그 이름이 있어야 한다. 문서에 두지 않는 내부용 설정(`KRX_COMMIT` 같은)은 스크립트의 `UNDOCUMENTED`에 이유와 함께 둔다. 절 제목, 표의 첫 칸, 노트만 읽으므로 그 밖의 문장은 자유롭다. 절이나 표를 찾지 못해도 실패한다. README.md와 docs/ 어디서든 인용(`>`) 줄 바로 다음의 표 줄도 실패한다: Markdown은 그 줄부터 빈 줄까지를 인용의 글로 읽어 표가 거기서 끊긴다. 표 중간에 노트를 두면 노트 뒤에 빈 줄과 그 표의 머리 줄을 다시 둔다. CI도 같은 검사를 한다(job `lint`).

PR에는 `pr-refs` 검사(`.github/workflows/pr-refs.yml`, `scripts/check_pr_refs.py`)가 따로 돈다. 본문에서 "Refs" 뒤에 적은 이슈를 머지가 닫으면 실패하고, 어느 이슈가 어떤 구절 때문에 닫히는지 출력한다. 닫힌다고 보는 경우는 둘이다: GitHub GraphQL API의 `closingIssuesReferences`에 그 이슈가 있거나, 본문에 닫는 키워드(close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved)와 그 이슈 번호가 이어 붙어 있을 때다. GitHub는 백틱 안이나 "not" 뒤의 키워드도 읽어서, #340과 #341의 머지가 예약 작업이 아직 댓글을 다는 #310과 #314를 닫았다. 본문을 고치면 검사가 다시 돈다. PR의 Development 사이드바에서 이슈를 잇거나 끊는 것은 검사를 다시 돌리지 않는다(GitHub의 `pull_request` 이벤트에 그런 종류가 없다). 그런 링크는 다음 실행(push, 본문 수정, 다시 열기) 때에야 잡히므로, 검사가 지키는 것은 본문이다. 사이드바에서 이슈를 이었을 수 있으면 머지 전에 아래 명령으로 다시 확인한다. PR을 API로 읽으므로 위의 로컬 검사에는 없다. 열린 PR은 `python3 scripts/check_pr_refs.py codeyoma/krx-quant <PR 번호>`로 확인한다.

PR에는 `pr-docs` 검사(`.github/workflows/pr-docs.yml`, `scripts/check_pr_docs.py`, #507)도 따로 돈다. PR이 `src/` 아래 파일을 바꿨는데 `docs/` 아래 파일, `README.md`, `.env.example` 중 아무것도 바꾸지 않았으면 실패하고, 바뀐 `src/` 파일을 출력한다. 문서에 고칠 것이 없는 변경(설정, 명령, 필드, 문서가 적은 동작이 그대로인 리팩터링 같은)은 본문에 그 줄만 있는 `Docs: none (<이유>)`를 두면 통과한다. 이유가 비었거나 `<이유>`를 그대로 둔 줄은 통과하지 않는다. 바뀐 파일과 본문은 REST API에서 지금 것을 읽고, 이름을 바꾼 파일은 옛 경로와 새 경로를 다 본다. 본문을 고치면(`edited`) 검사가 다시 돈다. 리뷰에서 Codex가 문서가 빠진 PR을 8번 짚어서 생긴 검사다. 열린 PR은 `python3 scripts/check_pr_docs.py codeyoma/krx-quant <PR 번호>`로 확인한다.

## 수집기 실행

설정은 환경 변수와 `.env`에서 읽는다. `.env.example`을 `.env`로 복사해 채운다.

```bash
uv run krx-quant collect
```

`Ctrl+C`(또는 SIGTERM)로 멈추면 남은 데이터를 쓰고 끝난다. 녹화하는 피드와 구독 순서는 [docs/kis-feeds.md](docs/kis-feeds.md), 녹화 형식은 [docs/events.md](docs/events.md)에 있다.

실시간 구독 한도를 다시 재려면 수집기를 멈춘 상태에서 실행한다(앱키 하나에 세션 하나라서):

```bash
uv run krx-quant measure-subscriptions
```

장애 처리:
- **재연결**: KIS가 연결을 끊으면 1초부터 두 배씩 늘려 최대 60초 간격으로 재연결하고, 모든 구독을 다시 등록한다. 20시간이 지난 승인키는 재연결할 때 새로 받는다.
- **침묵 감시**: KIS WebSocket에 60초(`KRX_WS_SILENCE_SECONDS`) 동안 프레임이 하나도 오지 않으면(쉴 때도 오는 PINGPONG까지) 닫히지 않은 채 죽은 연결로 보고, 닫은 뒤 위처럼 재연결한다. 끊김과 같이 결측(`disconnected: silent 60s`)과 텔레그램 알림이 남는다(#308).
- **결측 기록**: 끊긴 구간과 연속매매 시간에 조용해진 피드는 `gaps` 계층에 기록한다. 거래일에는 개장 후 한 번도 데이터가 오지 않은 구독도 지연으로 본다. 휴장일에는 판정하지 않는다. 밤(18:00–06:00)에만 오는 야간선물(#264)은 조용해도 지연으로 보지 않는다.
- **토큰 재사용**: REST 접근 토큰은 `state/kis_token.json`(권한 600)에 캐시해서 재시작해도 다시 발급하지 않는다. 만료 1시간 전부터 갱신하고, 발급 간격은 61초 이상으로 둔다(KIS는 분당 1회).
- **텔레그램 알림**: `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`를 넣으면 장애가 시작될 때 한 번, 복구될 때 한 번 텔레그램으로 알린다. 넣지 않으면 로그만 남긴다.
- **로그**: 모든 프로세스의 로그 줄이 stdout에 나온다(`docker compose logs`, 다음 배포까지). `KRX_LOKI_URL`, `KRX_LOKI_USER`, `KRX_LOKI_TOKEN`을 넣으면 같은 줄을 가려서 Grafana Cloud Loki로 보내고, 로그는 거기 남는다(#358). 엔진이 띄운 밤 학습과 예약 작업의 출력도 간다. 묶어서 보내고, 실패하면 다시 보내고, 넘치면 버리고 센다. 엔진은 Loki를 기다리지 않는다. 에이전트는 거기서 질의하고([docs/agents/server-logs.md](docs/agents/server-logs.md)), Grafana의 알림 규칙이 엔진이 스스로 보내지 못하는 것(엔진 로그 끊김 등)을 텔레그램으로 보낸다([docs/operations.md](docs/operations.md#grafana-cloud-loki로-보내는-로그-358)).
  - WebSocket 끊김 알림은 끊긴 뒤 30초가 지나도 다시 붙지 않았을 때만 보낸다. KIS가 하루 몇 번 닫았다가 몇 초 안에 다시 붙는 끊김은 알리지 않고, 로그(WARNING)에 그날 수와 함께 남긴다. 결측은 그대로 기록한다(#488).
  - 재연결 복구 알림은 연결이 60초(`KRX_RECOVERY_HOLD_SECONDS`) 이상 유지된 뒤에 보낸다. 그래서 끊겼다 붙기를 반복해도 알림이 매번 가지 않는다.
  - 지연 알림은 새로 지연된 피드들을 한 메시지로 묶어 보내고, 다시 들어온 피드도 묶어서 알린다.
  - 예측 신호(`[신호] ...`)는 선택이다. `KRX_SIGNAL_ALERT=on`이면 최종 합의가 충분히 확실하고 정식 알고리즘 N개 이상이 같은 방향일 때 보낸다. VI·서킷브레이커, 기준가 없음, 목표 시각 지남이면 보내지 않는다([docs/engine-api.md](docs/engine-api.md#텔레그램-신호-알림-19)).

거래 일정과 1분봉:
- **거래 일정**: KIS 휴장일 API로 거래일을 판정하고 `state/market_calendar.json`에 캐시한다. 첫 실행에 지난 1년을 받고, 그다음부터는 하루 한 번 갱신한다. 연초 첫 거래일과 수능일은 10:00에 연다.
- **1분봉 백필**: 수집기가 시작할 때와 매일 20:30에 지난 365일 중 빠진 거래일의 1분봉을 REST로 받아 `bars` 계층에 쓴다. 첫 실행에는 1년치를 받는다(종목당 약 1,500회 조회라 초당 8회로 약 3분, 기본 5종목이면 약 15분). KOSPI200 선물과 달러선물은 날마다 최근월물을 받아 연속 1분봉으로 이어 붙인다(상품당 약 1,250회, 약 2분 반).

수집기를 멈춘 상태에서 백필만 한 번 돌릴 수도 있다(둘이 동시에 돌면 REST 한도를 나눠 쓰지 못한다):

```bash
uv run krx-quant backfill
```

장 마감 업로드:
- `KRX_OBJECT_STORE`(`s3://버킷/engine` 또는 로컬 폴더)를 넣으면, 수집기가 거래일마다 장 마감 15분 뒤와 매일 21:00에 녹화한 날을 올린다. 시작할 때는 지난날 중 아직 안 올린 것을 올린다.
- 파티션마다 part 파일을 `data.parquet` 하나로 합쳐 올린다. 다시 돌려도 결과가 같다. 손으로 돌릴 때는 다음 명령을 쓴다.

```bash
uv run krx-quant archive
```

예측과 엔진 API:
- 수집기는 받은 이벤트로 1분봉 마감마다 기준 예측 4종(항상 보합, 직전 창의 도달 정답, 그날 도달 정답 비율대로 무작위, 시간대 기후), 알고리즘(Newton 추세 추종, Gauss 역추세, Granger 이웃 관찰, Markov 국면 탐정, Turing 만능 학습), 합의 2종(Condorcet 빠른·최종)의 예측을 낸다. 4분 뒤 그 봉 마감의 도달 정답(#160: 4분 안에 매수 1호가가 중간가 + k틱에 닿으면 상승, 아니고 매도 1호가가 − k틱에 닿으면 하락, 아니면 보합) 하나로 채점한다(#170). 결과는 `state/engine.sqlite3`에 쌓인다.
- 알고리즘은 모두 수습으로 시작한다. 매일 장 마감 10분 뒤 상태(수습, 정식, 벤치, 은퇴, 복제)를 점검하고, 정식 알고리즘만 합의에 들어간다([docs/engine-api.md](docs/engine-api.md#상태-관리-17)). 판정과 합의 가중치는 도달 정답으로 학습한 모델의 예측만 본다. 정답이 바뀐 날(#170 배포) 모든 알고리즘이 수습으로 돌아가, 새 정답 모델이 올라간 거래일부터 다시 센다([docs/operations.md](docs/operations.md#정답이-바뀌는-날-170)).
- 순위표(`/v1/leaderboard`)와 알고리즘 상세 통계(`/v1/algorithms/<이름>/stats`)를 낸다. 방향 정밀도, 커버리지, 기준 예측 대비 lift, 닿음 손익(정답과 같은 규칙, 연구용. 하락을 주식 공매도로 친 것과 인버스 상품으로 친 것, #267), Brier 점수, 보정 곡선을 신뢰구간과 함께 보여준다. 상세 통계는 기준율 대비 실력(kappa, Gerrity, 균형 맞힘률, 독립성 검정)과 변동성 3분위별 성적도 낸다(#262). 알고리즘마다 거래일별 맞힘률, 상승·보합·하락 정밀도, 기후 기준 예측 대비 lift, log loss, 그날 상태와 정식 알고리즘의 하락 표시, 합의와 구성원의 비교는 `/v1/leaderboard/days`에 있다(#561). 알고리즘 목록(`/v1/algorithms`)은 사전 검증을 기다리거나 거부된 알고리즘도 보인다(#561).
- 같은 봉 마감에 방향 말고 변동폭(4분 동안 통합 중간가의 최고 − 최저, #180)도 예측한다: 기준 예측 2종(시간대 기후, 직전 4분 지속)과 첫 모델 Mandelbrot(#181, HAR 방식 로그 범위 회귀). Mandelbrot은 사전 검증(`prevalidate mandelbrot`)을 통과해야 돈다. 순위표는 `/v1/leaderboard?kind=range`다([docs/engine-api.md](docs/engine-api.md#변동폭-177-180)).
- 장벽(#183)도 예측한다: 봉 마감 중간가 ± 폭 w(1분 변화의 지수가중 표준편차 × √4, 비용 k보다 좁지 않게)에 둔 위·아래 장벽 중 4분 안에 어느 쪽에 먼저 닿을지(위 먼저·시간 만료·아래 먼저, 삼중 장벽). 기준 예측 2종(시간대 기후, 직전 4분 지속)과 첫 모델 Breiman(#184, LightGBM 3분류)이 예측하고, 연구용 장벽 손익을 함께 낸다. Breiman은 사전 검증(`prevalidate breiman`)을 통과해야 돈다. 순위표는 `/v1/leaderboard?kind=barrier`다([docs/engine-api.md](docs/engine-api.md#장벽-178-183)). 같은 장벽을 시간 장벽 15·30·60분으로 둔 `barrier_15m`, `barrier_30m`, `barrier_60m`(#263, 폭은 σ̂₁ × √H)도 기준 예측 2종씩과 지평마다의 Breiman(`breiman-15m`, `breiman-30m`, `breiman-60m`, 사전 검증 뒤)으로 예측한다.
- 봉 마감마다 종목 하나에 판정 하나를 낸다(#676): 매수·매도·대기와 최대 보유 시간, 목표가·손절가. 예측을 낸 장벽 알고리즘(상태와 상관없이)의 세 확률을 지평(4·15·30·60분)마다 계열별로 같은 무게로 합치고, 매도 1호가에 사서 목표·손절·시간 만료 때의 매수 1호가에 파는 기대 순손익(trader의 왕복 비용)으로 정한다. 매도는 trader가 들고 있으면 청산, 아니면 매수 피하기다. 구성원마다 상태, 증거 수, 기울기(위·아래·없음과 세기)를 보이고, 방향·변동폭 예측은 옆에 보이기만 한다. 엔진 DB `verdicts`에 저장하고 `/v1/verdict`로 낸다([docs/design.md](docs/design.md) 5장 "판정").
- 하루에 한 번(거래일 08:50) 그날 KRX 시가 단일가 → 종가 단일가의 방향(상승·보합·하락, 보합 폭은 종목 변동성으로 세 클래스가 1/3씩 되게)도 예측한다(`open_to_close`, #264): 기준 예측 3종(늘 보합, 기후, 어제 방향)과 첫 모델 Cox(다항 로지스틱 회귀, 밤사이 미국 반도체·전일 수급·NXT 프리마켓 등, KRX 일봉 10년 walk-forward 사전 검증 뒤). 정답은 그날 15:30 종가 단일가 가격(KIS KRX 1분봉, 15:40에 받는다)이 저장된 뒤(15:45부터) 쓴다(정답 버전 2, #317. 그 저녁 저장한 KRX 일봉의 종가는 KIS의 잠정 값, 곧 애프터마켓의 마지막 가격이고, KIS가 확정한 종가는 종가 단일가다. 수집은 마지막 5개 봉을 다시 받는다, #264). 순위표는 `/v1/leaderboard?kind=open_to_close`다([docs/engine-api.md](docs/engine-api.md#하루-한-번-예측-종류-264)).

  > **바뀜** (2026-10-15, #209): "지금은 기준 예측 2종(시간대 기후, 직전 4분 지속)이 예측하고" → 첫 모델 Breiman도 예측한다(사전 검증 뒤 수습). 이유: #184가 넣었다.
- 알고리즘은 녹화된 날은 녹화 이벤트(호가로 만든 도달 정답)로, 백필만 있는 날은 1분봉(1분봉 규칙의 도달 정답)으로 학습한다. 모델은 `state/models/`에 저장되고, 실행 중인 엔진은 30초마다 확인해 바뀐 모델로 바꾼다.
- `KRX_TARGET_VERSION`(#169)은 3(도달 정답, #170. 뒤집힌 호가도 센다, #187)이고 고를 수 있는 값은 이것 하나다. `2`(뒤집힌 호가를 버린 도달 정답)와 `1`(옛 정답, 4분 뒤 중간가 변화)은 설정 오류다. 버전마다의 뜻은 [docs/engine-api.md](docs/engine-api.md#채점)의 "정답 버전"에 있다.

> **삭제됨** (2026-11-10, #669): `KRX_TARGET_VERSION=1`. "학습 정답, 알고리즘의 공통 정규분포 함수, 기준 예측(3종), 사전 검증이 옛 정답(4분 뒤 중간가 변화)을 따르지만, 채점은 그대로 도달 정답이라 수습이 세어지지 않는다." 이유: 서버는 3으로 돌고, 1의 분기가 14개 모듈에 남아 있었다. 사용자가 2026-11-09에 지우기로 정했다(#659). 대신: 3. `1`은 대신할 값을 말하며 거부한다. 저장된 버전 1의 기록은 계속 읽는다.

> **바뀜** (2026-10-17, #260): `KRX_TARGET_VERSION` 기본 2 → 3, `2`는 설정 오류. 이유: 닿음이 뒤집힌 호가(거래소 사이에 겹친 매수·매도 1호가)도 실제 거래 가격으로 세게 되어 정답 버전이 올랐다(#187).

> **바뀜** (2026-10-14, #193): 기준 예측 3종(항상 보합, 직전 4분 방향 지속, 클래스 비율 무작위)과 "4분 뒤 두 기준(봉 마감, 표시 시점)으로 채점", "기준 3종 대비 lift, 중간가 손익(연구용, 스프레드·체결 없음)", "녹화된 날은 중간가 기준, 백필은 종가 기준으로 학습", `KRX_TARGET_VERSION` 기본 1 → 위. 이유: 정답을 도달 정답 하나로 바꿨다(#160).

```bash
uv run krx-quant fit newton
uv run krx-quant fit gauss
uv run krx-quant fit granger
uv run krx-quant fit markov
uv run krx-quant fit turing
uv run krx-quant fit mandelbrot        # 변동폭(#181): 변동폭 정답으로
uv run krx-quant fit breiman           # 장벽(#184): 장벽 정답으로
uv run krx-quant fit breiman-15m       # 15분 장벽(#263): 그 종류의 정답으로
uv run krx-quant fit cox               # 시가→종가(#264): KRX 일봉 10년으로
uv run krx-quant train                 # 밤 학습 한 번: KRX_OBJECT_STORE의 최근 날로 학습·검증·승격 목록
uv run krx-quant rollback granger      # 한 단계(직전 모델로) 되돌리기. 바로 전 모델만 남는다(#361)
```
- 서버에서는 수집기가 매 거래일 21:30에 밤 학습을 돌리고, 다음 거래일 08:00에 이긴 모델로 바꾼다([docs/operations.md](docs/operations.md)).
- 서버는 매일 밤 22:30부터, 밤 학습이 끝난 뒤 때가 된 예약 작업(자동 매매의 밤 리포트, 녹화 10거래일 뒤의 사전 검증 등)을 하나씩 돌리고 결과를 GitHub 이슈 댓글로 남긴다([docs/operations.md](docs/operations.md#예약-작업-215), #215). **바뀜** (2026-11-04, #506): 예의 "진단" → 밤 리포트. 이유: 진단 작업(`diagnose-203-187`)은 할 일을 마치고 `JOBS`에서 빠졌다(#505).
- 서버는 매일 06:00(KST)에 main의 새 커밋을 받아 스스로 다시 띄운다. 그 커밋에서 `ci` workflow가 통과했을 때만 받고, 아니면 돌던 버전을 그대로 둔다(#541). 새 버전이 뜨지 않으면 이전 커밋으로 되돌리고 텔레그램으로 알린다(`deploy/auto-update.sh`).
- 서버의 OS가 멈추면 커널이 스스로 재부팅하고(`deploy/kernel-recovery.sh`), 그래도 Lightsail의 인스턴스 상태 검사가 15분 넘게 실패하면 GitHub Actions의 watchdog(`.github/workflows/server-watchdog.yml`, `scripts/server_watchdog.py`)이 인스턴스를 재부팅하고 이슈로 알린다([docs/operations.md](docs/operations.md#서버가-멈추면-385), #385).
- 엔진 API는 `http://127.0.0.1:8080/v1/status`처럼 읽는다(`KRX_API_HOST`, `KRX_API_PORT`). 서버에서는 Tailscale 주소로만 열린다. 엔드포인트와 예측·채점 규칙은 [docs/engine-api.md](docs/engine-api.md)에 있다. 그날의 매크로 지표와 국면은 `/v1/macro`다. 예측, 합의, 채점, KIS 상태, 알고리즘 상태 변경은 WebSocket `/v1/stream`으로 바로 받는다.
- 응답 형식은 JSON 스키마 [schemas/engine-api-v1.json](schemas/engine-api-v1.json)이 정한다. 화면과의 계약이다. 필드를 바꾸면 스키마도 고쳐야 계약 테스트(`tests/test_api_contract.py`)를 지난다.
- 녹화된 하루를 같은 엔진으로 다시 돌려 별도 DB(`state/replay-<날짜>.sqlite3`)에 쓴다. KIS 키는 필요 없다.

```bash
uv run krx-quant replay 2026-10-12
```

### 상시 서버 (Docker)

서버는 `compose.yaml`로 띄운다. 개통, 키 교체, 장애 대응은 [docs/operations.md](docs/operations.md)에 있다.

```bash
docker compose up -d --build
```

서비스는 둘이다: 수집기·엔진(`engine`)과 자동 매매 trader(`trader`, #394: 모드는 `paper`, `mock`, `live`). 같은 이미지의 다른 컨테이너라 하나가 죽어도 다른 하나는 돈다. 엔진 API의 포트만 Tailscale 주소에 열리고, 수집기가 trader에게 호가를 보내는 포트(8081)는 compose 네트워크 안에만 있다.

> **삭제됨** (2026-10-08, #33): `docker build -t krx-quant .` 뒤 `docker run --rm --env-file .env -u "$(id -u):$(id -g)" -v "$PWD/data:/data" -v "$PWD/state:/state" krx-quant collect`로 띄우던 안내. 이유 기록 없음. 대신: `compose.yaml`(엔진 포트를 Tailscale 주소에만 연다)과 `deploy/setup-server.sh`.

로컬 Docker에서 가짜 KIS에 붙여 수집 → 녹화 → 업로드까지 확인하려면 다음을 실행한다. 엔진 API가 응답하는지, 업로드를 두 번 돌려 두 번째에 바뀌는 것이 없는지도 본다. 설정은 `deploy/smoke.env`이고, 실제 `.env`, `data/`, `state/`는 건드리지 않는다.

```bash
deploy/smoke-local.sh
```

### 키 없이 확인하기 (가짜 KIS)

```bash
uv run krx-quant fake-kis --port 18080
```

다른 터미널에서 출력된 주소와 테스트 키로 수집기를 붙인다.

```bash
KIS_APP_KEY=test-app-key KIS_APP_SECRET=test-app-secret KIS_REST_URL=http://127.0.0.1:18080 KIS_WS_URL=ws://127.0.0.1:18080 uv run krx-quant collect
```

가짜 서버는 구독에 응답하고 10초마다 PINGPONG을 보낸다. `--trades`를 붙이면 구독한 통합 체결(`H0UNCNT0`) 종목마다 1초에 한 번 가짜 체결도 보낸다. 다른 시세 메시지는 테스트에서만 흘린다. Docker 안에서 다른 컨테이너가 붙게 하려면 `--host 0.0.0.0`을 쓴다(`deploy/compose.smoke.yaml`).

### 화면을 만들 때 (데모 엔진)

```bash
uv run krx-quant demo-engine            # http://127.0.0.1:8090/v1/status
uv run krx-quant demo-engine --pace 2   # 10:00에 열고 10:30까지 1분을 2초마다 이어 재생하며 스트림을 보낸다
```

서버 없이 엔진 API를 띄운다(#20). 지어낸 아침(2026-10-12 09:00–10:30, 3종목, KOSPI·선물·달러선물, 서킷브레이커 1단계와 VI 한 번)을 실제 엔진으로 재생하고, 실제 API 코드로 답한다. 코스피가 09:04에 8.5% 떨어져 09:06–09:36에 서킷브레이커 1단계가 걸린다. 그 30분은 세 종목 모두 체결과 호가가 없어 1분봉이 비고, 000660은 09:40:10–09:42:20에 VI(동적)가 걸린다. 둘 다 `GET /v1/symbols/{종목}/special`과 `status.special`에 나온다(#236). 선물은 떨어지지 않아 사이드카는 없다. 늘 같은 응답이다. 다만 `status.cpu`의 시간은 실제로 재서 실행마다 조금 다르다. 시계는 10:30에 멈춰 있고, `status.mode`는 `demo`다. 모든 알고리즘을 정식(`active`)으로 두어 합의도 나온다. 서버처럼 `cox-5d`는 사전 검증에 거부된 것(지어낸 리포트), `breiman-60m`은 사전 검증을 기다리는 것으로 두어 `GET /v1/algorithms`의 `state`가 `refused`, `waiting`인 항목도 보인다(#561). 날짜별 성적(`GET /v1/leaderboard/days`)은 점검을 마친 10-07, 10-08이다(방향 알고리즘은 기본 모델로 돌아 순위표처럼 세지 않고, 기준 예측·합의·Cox의 날이 있다). 그날 07:30 매크로 읽기는 위험회피(`risk_off`)로 정해져 있다. KIS 상태(`status.kis`, 결측 피드 하나)와 Newton의 모델 이력도 가상 값으로 보인다. 그 앞 두 거래일(2026-10-07, 10-08. 10-09는 한글날 휴장)의 14:00–15:20도 미리 재생해 둔다(#108). 그날들의 예측과 채점은 DB에, 1분봉은 백필과 같은 파일로 있어 차트를 지난 날로 스크롤해 볼 수 있다. SMT 다이버전스(#521)는 `GET /v1/symbols/{종목}/smt`로 나온다: 10-08과 10-12 아침은 엔진이 장중에 남긴 것(`live`)이고, 10-07은 그날 live 사건을 지워 두어 채운 1분봉(주식과 KOSPI200 선물)으로 다시 계산한 것(`reconstructed`)이다. 종목마다 10-08까지 지어낸 일봉 500개와 그것을 묶은 주·월·년봉도 있어 차트의 "일·주·월·년" 보기를 거슬러 볼 수 있다(#111, #112). 같은 500일에는 지어낸 일별 수급 8종(공매도, 개인·기관·외국인 순매수, 외국인 보유수량·지분율, 대차·신용잔고)도 있어 `GET /v1/symbols/{종목}/supply`로 하단 지표를 그려 볼 수 있다(#226. 10-12 아침 그날의 값은 아직 없다). KRX 마감 흐름(#305)도 있다: 16종의 지어낸 순자산(10-08의 0198D0 하나는 없다)으로 10:30의 엔진 기준가에서 계산한 그날 수요와, 일봉에서 지어낸 10-08까지 20거래일의 표(마지막 8일은 저장한 15:20 수요)가 `GET /v1/symbols/{종목}/closing-flow`, `/closing-flow/days`로 나온다. 상품 이름도 지어낸 것이다. 자동 매매 trader(#393)의 저장소도 지어낸 것이다: 005930의 매매(10-07·10-08 오후와 10-12 아침, 10:05까지 모의, 그 뒤 실전, #398), 10:30의 상태(실전 trader가 돌고 아침의 마지막 매매를 들고 있다), 모드마다 날마다의 장부, 10-08 화면의 새 진입 정지와 10-12 서버의 재개, 정책 둘(10-07 밤의 챔피언 1, 10-08 밤의 도전 정책 2와 그 모의 매매. 칸마다 밤 맞추기의 수치와 확률 보정, 뺀 칸 하나, #475)과 그것으로 잰 실전 관문 진행, 10-08 밤에 시작한 평가 버전의 검토 주기(#469), 위험 규칙의 상태와 매매마다의 실행 비용(#475)이 `GET /v1/trader`, `/v1/trader/days`, `/v1/trader/trades`, `/v1/trader/policies`, `/v1/symbols/005930/trader-marks`로 나온다(#397, #400). 매매마다 그 사기 결정도 장부별로 `GET /v1/trader/decisions`로 나온다(#584. 건너뛴 결정과 고르기는 지어내지 않아 θ와 p*가 `null`이고, 데모의 엔진 DB는 `state/engine.sqlite3`가 아니어서 정답도 `null`이다). 봉 마감마다의 판정(`GET /v1/verdict`, #676)도 나온다: 4·15·30분 Breiman을 지어낸 모델로 돌려(60분은 서버처럼 대기) 매수·매도·대기가 모두 나오고, 아침의 마지막 매매를 들고 있는 동안은 청산이나 보유 대기로 읽는다. 정지를 보내면 쓰기는 하지만 읽을 trader가 없다. Fincept 화면의 엔진 주소를 `http://127.0.0.1:8090`으로 바꿔 붙인다. DB는 `state/demo.sqlite3`(`--db`)에 새로 만들고, 매크로 기록, 모델 이력, 지난 날의 1분봉, 일봉, 일별 수급, trader 저장소는 그 옆 `state/demo-data/`(`macro/`, `state/models/`, `bars/`, `candles/`, `state/supply/`, `state/trader.sqlite3`)에 새로 쓴다. 응답은 실제 엔진과 같은 스키마를 지킨다.

> **바뀜** (2026-10-16, #236): 데모 아침은 09:00–10:30 내내 체결과 호가가 있었고 특수 상황은 VI 하나였다 → 09:06–09:36은 서킷브레이커 1단계로 체결과 호가가 없다. 이유: 화면이 체결 없는 분의 빈 봉과 서킷브레이커 띠를 데모로 보이고 캡처하게 하려고.

`--pace 초`를 주면(#21) 10:00까지만 미리 재생하고 API를 연 뒤, 10:30까지 1분을 그 초마다(10초씩 여섯 번에 나눠) 이어 재생한다. 그동안 스트림(`/v1/stream`)으로 예측, 합의, 채점, 만들어지는 봉(`bar`, #110)이 실제처럼 와서 화면의 스트림, 차트 갱신, 재연결을 시험할 수 있다. KIS 상태의 시각도 데모 시계를 따른다. 10:30에 끝나면 `--pace` 없이 띄운 데모와 같은 답을 낸다. 데모 아침의 최종 합의는 50%를 거의 넘지 않아 화면의 신호 알림은 나오지 않는다.

### 실제 KIS로 확인하기 (장중)

1. KIS Open API 실전 앱키와 시크릿을 `.env`의 `KIS_APP_KEY`, `KIS_APP_SECRET`에 넣는다. `KRX_SYMBOLS=005930`으로 둔다.
2. 평일 09:00–15:20 사이에 `uv run krx-quant collect`를 실행한다.
3. 로그에 구독 거부(`KIS rejected ...`)가 없는지 본다.
4. 1–2분 뒤 `Ctrl+C`로 멈춘다.
5. 다음 위치에 파일이 생겼는지 확인한다.
   - `data/events/date=<오늘>/feed=H0UNCNT0/symbol=005930/`
   - `data/events/date=<오늘>/feed=H0UNASP0/symbol=005930/`
6. 다음 명령으로 체결가와 시각이 HTS와 맞는지 확인한다.

```bash
uv run python -c "import glob, pyarrow.parquet as pq; d = sorted(glob.glob('data/events/date=*/feed=H0UNCNT0/symbol=005930'))[-1]; [print(r) for r in pq.read_table(d).slice(0, 5).to_pylist()]"
```

## 명령

모두 `uv run krx-quant <명령>`(서버에서는 `docker compose run --rm engine <명령>` 또는 `docker compose exec engine krx-quant <명령>`)이다. 설정은 환경 변수와 `.env`에서 읽는다([docs/operations.md](docs/operations.md#설정-값-전체)).

| 명령 | 하는 일 | KIS 키 |
|---|---|---|
| `collect` | 실시간 녹화, 예측·채점, 엔진 API, 업로드, 백필, 밤 학습 일정, 밤 예약 작업(#215), 라벨로 요청한 이슈에 서버 요약 댓글(#322). 시작할 때 API를 연 뒤 지난 예측의 빠진 도달 정답을 녹화로 채운다(#168). 멈출 때까지 돈다 | 필요 |
| `trade` | 자동 매매(#394): 챔피언 정책의 칸들(#399. 시작할 때와 매일 08:30에 엔진 API의 비용 지도(`KRX_TRADER_ENGINE_URL`)를 읽어 그 수치를 남긴다. **바뀜** (2026-11-03, #480): 비용 지도가 시장가 왕복으로 통과시킨 칸들(#395)이었다. 이유: spec #468 3절 "진입": 비용 지도 통과가 필요조건에서 빠졌다)의 예측을 엔진 DB에서 읽기만 하며 005930을 삼중 장벽으로 매매하고 `state/trader.sqlite3`에 남긴다: `KRX_TRADER_MODE`가 `paper`면 모의로, `mock`이면 KIS 모의투자 서버에(#405), `live`면 관문과 실행 점검을 넘었을 때 새 실전 계좌에(#408. 그 전에는 모의로, 판단이 바뀌면 08:30에 스스로 멈추고 Docker가 다시 띄운다). 호가는 수집기의 호가 스트림(`KRX_TRADER_QUOTES_URL`)에서 받는다. 서버에서는 compose 서비스 `trader`가 돌린다(수집기와 따로). 멈출 때까지 돈다([docs/operations.md](docs/operations.md#자동-매매-394)). 하루·누적 손실 한도(운용 상한의 10%, 20%)에 닿거나 화면에서 정지하면 멈춘다(#396) | 없어도 된다 |
| `trade resume`, `trade rearm` | 돌고 있는 trader에게 화면의 정지를 풀거나(`resume`) 누적 손실 한도를 다시 켜라고(`rearm`, 누적 손익을 그때부터 다시 센다) `state/trader.sqlite3`에 쓴다(#396). trader가 1초 안에 읽는다. 풀 것이 없으면 종료 코드 1이다. 서버에서 `docker compose exec trader krx-quant trade resume` | 없어도 된다 |
| `refit-policy [--report]` | 자동 매매의 정책을 다시 맞춘다(#399, #400): 먼저 도전 정책을 챔피언과 견주어 이긴 것을 챔피언으로 바꾸고(검토 날 장이 끝난 뒤에만, #469. **바뀜** (2026-11-02, #476): 장중이 아니면 어느 밤이든 → 20거래일마다의 검토 날 밤에만. 이유: spec #468 "버전과 검토 주기". #473부터 그 주기의 도전 정책 묶음을 한 번 판정하고 뽑히지 않은 것은 모두 내린다. **바뀜** (2026-11-03, #481): 도는 도전 정책을 10일째부터 판정하고 20일까지 이기지 못한 것을 내렸다 → 묶음을 한 번. 이유: spec #468 5절), 그 밤 비용 지도(`KRX_TRADER_ENGINE_URL`)에 이름이 있는 칸마다 문턱 θ를 저장된 20일의 예측과 녹화로 맞춘(#472: 1주씩 따로 잰 신호당 순손익의 날 단위 하한이 0을 넘는 가장 낮은 θ, 넣은 칸들의 정책 재생도 하한이 0을 넘어야 저장. **바뀜** (2026-11-03, #480): 비용 지도를 통과한 칸마다 로그 성장으로 맞췄다. 이유: spec #468 3절 "진입") 정책 버전을 `state/trader.sqlite3`에 저장한다(엔진 DB는 읽기만. **바뀜** (2026-11-03, #478): 칸마다 Kelly 비율 λ도 저장된 예측과 정답으로 맞췄다 → θ만, 녹화한 날을 trader의 체결 규칙 그대로 재생해 맞춘다. 이유: 손실 예산이 수량을 정한다(#471), 맞추기는 #470부터 녹화를 재생한다). 맞춘 정책은 다음 묶음을 기다리고, 끝에 검토 날 밤이면 기다리는 정책 가운데 새것부터 셋까지로 다음 주기의 묶음을 만든다(#473). `--report`는 그 뒤 그날의 리포트(`trader-report`)를 그 출력 위에 찍는다: 예약 작업 `trader-report`가 거래일 밤마다 돌려 리포트 이슈 #417에 남긴다(#402) | 없어도 된다 |
| `trader-report [--day YYYY-MM-DD]` | 자동 매매의 그날 리포트를 출력한다(#402): 그날의 장부와 챔피언의 매매(#475부터 다리마다 실행 비용), 매매할 칸이 없던 까닭, 정책별 모의 성적과 도전 정책의 묶음 판정(#473), 그 밤의 교체·묶음 판정·내림·새 묶음·맞춘 정책, 실전 관문, 한도와 정지(#475부터 위험 규칙의 그날 상태). 둘째 줄 `에이전트 차례:`가 에이전트를 부르는지와 그 까닭을 말한다. **가격, 계좌번호, 주문번호, 키는 출력하지 않는다.** `state/trader.sqlite3`를 읽기만 한다. 날을 주지 않으면 지금이 그 밤에 드는 거래일이다([docs/operations.md](docs/operations.md#자동-매매-394)의 "밤 리포트 (#402)") | 없어도 된다 |
| `check-mock-trades [--day YYYY-MM-DD]` | mock 모드로 돈 거래일 하나의 trader 주문을 장부와 KIS 주문 체결 조회로 견준다(#405): 주문마다 매수·매도, 주문 수량, 체결 수량, 체결가 차이(틱), KIS에 없는 장부의 주문과 장부에 없는 KIS의 주문. trader 자신의 모의 키(`KRX_TRADER_KIS_*`)로 모의투자 서버에 조회만 한다. **주문번호, 계좌번호, 키, 가격은 출력하지 않는다.** 날을 주지 않으면 지금이 그 밤에 드는 거래일이다. 종료 코드: 0 같다, 3 그날 장부에도 KIS에도 주문이 없다, 4 저장소나 KIS를 읽지 못함, 5 다르다. 서버에서는 예약 작업 `check-mock-trades-405`가 2026-11-02 밤에 돌려 #405에 남겼다(`같다`)([docs/operations.md](docs/operations.md#kis-모의-405)) | 없어도 된다(trader의 모의 키로 묻는다) **바뀜** (2026-11-04, #506): "mock 거래일 밤에 돌려 #405에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `fill-notice-layout` | mock trader의 체결 통보 기록기(#484)가 `state/fill_notices.sqlite3`에 남긴 체결 통보를 칸마다 잰다: 레코드를 그날 장부와 KIS 주문 체결 조회(trader 키, 읽기만)의 주문에 묶어, 칸마다 모양, 길이, 값의 가짓수, 그 주문의 값과 같은 수를 센다. **값, 주문번호, 계좌번호, HTS ID, 가격은 출력하지 않는다.** 종료 코드: 0 묶인 매수·매도·취소가 모두 있다, 3 아직 다 묶이지 않았다, 4 기록, 장부나 그날 KIS 조회를 읽지 못함. 손으로 돌린다([docs/operations.md](docs/operations.md#모의-체결-통보-칸-재기-484)). **바뀜** (2026-11-06, #587): "서버에서는 예약 작업 `fill-notice-layout-403`이 새 레코드가 있는 거래일 밤에 돌려 #403에 남긴다" → 손으로만. 이유: 2026-11-04 밤에 다 묶었고 #531이 23칸에 이름을 붙여, 그 작업이 `JOBS`를 떠났다 | 없어도 된다(trader의 모의 키로 묻는다) |
| `fill-notice-columns` | mock trader의 체결 통보 기록에서 칸 10(`CNTG_QTY`)과 칸 17(`ODER_QTY`)을 가른다(#648): `fill-notice-layout`처럼 묶은 레코드 가운데 수량이 1주보다 많고 체결이 있는 주문의 것에서, 칸마다 주문 수량·접수·체결 합의 일치 수와 판정을 보이고, 기록기가 끊긴 동안 통보가 빠진 주문 수를 센다. **값, 주문번호, 계좌번호, HTS ID, 가격은 출력하지 않는다.** 종료 코드: 0 쟀다, 3 통보 레코드가 묶인 그런 주문이 20건에 못 미침, 4 기록, 장부나 그날 KIS 조회를 읽지 못함. 서버에서는 예약 작업 `fill-notice-columns-648`이 그런 주문 20건이 기록된 첫 밤에 돌려 #648에 남긴다([docs/operations.md](docs/operations.md#모의-체결-통보-칸-재기-484)) | 없어도 된다(trader의 모의 키로 묻는다) |
| `probe-live-account` | trader의 실전 계좌를 조회만으로 읽는다(#408, `KRX_TRADER_LIVE_KIS_*`): 현재가, 잔고, 매수가능조회, 그날 주문 체결 조회. trader가 읽는 필드마다 실전의 답에 있는지와 그 꼴을 보이고, 결과를 trader 저장소(`live_probes`)에 남긴다. live 모드는 이것이 한 번 통과해야 주문한다. **주문하지 않는다.** 키, 계좌번호, 주문번호, 값은 출력하지 않는다. 서버에서는 예약 작업 `probe-live-account-408`이 실전 키가 있는 거래일 밤에 통과할 때까지 돌려 #417에 남긴다([docs/operations.md](docs/operations.md#실전-408)) | 없어도 된다(trader의 실전 키로 묻는다) |
| `probe-kis-orders` | trader의 모의 키(`KRX_TRADER_KIS_*`)로 KIS 모의투자 서버에 프로브 주문을 낸다(#403): 005930 1주의 매수와 매도 체결, 하한가 지정가 매수와 그 취소, 거부되어야 할 주문 셋, 체결 통보. 서버가 모의투자 서버가 아니면 아무것도 보내지 않고, 거래일 09:05–15:15에만 주문한다. 응답의 코드와 필드 모양만 출력한다(키, 계좌번호, 주문번호, 시세 값은 없다). 손으로만 돌리고, trader가 mock으로 돌고 있으면 돌리지 않는다(같은 모의 계좌로 주문한다). 단계와 종료 코드는 [docs/operations.md](docs/operations.md#kis-모의투자-주문-확인-403)에 있다 | 없어도 된다(trader의 모의 키로 주문한다) |
| `doctor [--api 주소]` | 서버 요약을 출력한다(#322): 엔진의 `commit`·`started_at`, KIS 연결(`connected`, `last_frame_at`, `seconds_since_frame`)과 피드·키별 `last_received_at`·`gap`, 필드가 모자라 raw에만 남긴 오늘의 프레임 수(`short_frames`, #381), REST 조회의 오늘 시도·실패·장애 수와 끝점·오류 코드별 실패(`polls`, #441), 오늘의 PINGPONG(시간별 수, 마지막, 가장 긴 간격), 예약 작업(마지막 실행, 끝까지 돈 적 없는 작업, 올리지 못한 결과), 디스크와 메모리, 밤 학습(#355), Loki 전송(켜짐·꺼짐, 보낸·버린 줄, 마지막 성공과 오류의 종류, #358). 엔진 상태는 엔진 API(`/v1/status`)에 묻는다: 이 컨테이너의 것, 그다음 compose의 `engine` 서비스(`docker compose run`). 닿지 않으면 DB의 마지막 시작을 쓴다. 엔진이 메모리에 둔 최근 로그 줄은 다른 프로세스라 읽지 못한다고 적고, 엔진의 전체 줄이 Loki에 있다고 적는다. **시세 값은 출력하지 않고**, 예약 작업의 댓글처럼 가린다. 읽기만 한다. 서버에서는 `docker compose exec engine krx-quant doctor`. 에이전트는 이슈에 `needs-server-snapshot` 라벨을 붙여 같은 요약(엔진의 최근 로그 줄 포함)을 댓글로 받는다([docs/operations.md](docs/operations.md#서버-상태-보기-322)) | 없어도 된다 |
| `measure-subscriptions` | 세션당 실시간 구독 한도를 잰다. 수집기를 멈추고 실행한다 | 필요 |
| `probe-candles` | **삭제됨** (2026-11-06, #567): KIS 기간별 시세(`FHKST03010100`, 일·주·월·년봉)가 주는 것(시장 구분별 응답과 필드, 한 번에 주는 개수, 거슬러 가는 끝, 수정주가 인자)을 재던 명령(`candle_probe.py`). 이유: 결론이 났다(#111, 2026-10-14 실측, [docs/kis-feeds.md](docs/kis-feeds.md#기간별-시세-일주월년봉)). 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 필요 |
| `probe-supply` | 일별 수급 지표(#226)를 줄 KIS 조회 7개(공매도, 투자자별 순매수, 외국인 보유·소진율, 대차·신용잔고)와, `TOSS_CLIENT_ID`·`TOSS_CLIENT_SECRET`이 있으면 토스증권 Open API 조회 5개(#235)를 불러 출력한다: 종목·ETF의 시장 구분별 응답, 한 번에 주는 행 수와 날짜, 필드마다 비었는지·0인지·정수(수량)인지·소수(비율)인지, 첫 종목을 거슬러 간 끝(토스는 기록의 부분마다 어느 날부터 null이 아닌지도), 같은 값을 주는 조회끼리(토스와 KIS도) 같은 날 수. **시세 값은 출력하지 않는다**(공개 이슈에 올라간다). 읽기만 하고 KIS는 0.5초, 토스는 0.2초 간격으로, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다(몇 분). 서버에서는 예약 작업 `probe-supply-226`이 2026-10-16 밤에 돌려 #226에 남겼다([docs/kis-feeds.md](docs/kis-feeds.md#일별-수급-226), [docs/toss-feeds.md](docs/toss-feeds.md), [docs/operations.md](docs/operations.md#일별-수급-출처-확인-226)). 하나도 답을 못 받으면 종료 코드 1 | 필요 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에 돌려 #226에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-net-buys` | **삭제됨** (2026-11-06, #567): KIS 투자자 순매수를 KRX(`J`)와 통합(`UN`)으로 시기마다 묻고 저장된 KIS 수급을 세던 명령(`net_buys_probe.py`, #264). 이유: 결론이 났다(#297, 2026-10-19 밤 `probe-net-buys-j-264`, [docs/operations.md](docs/operations.md#krx-순매수-확인-264)). 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 필요 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에 돌려 #264에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-close` | **삭제됨** (2026-11-06, #567): 최근 1년의 KRX(`J`) 일봉 종가를 KRX 1분봉의 단일가 봉과 녹화한 단일가에 견주고, 최근 10일을 KIS가 지금 주는 일봉과, 수집기가 바꾼 봉의 기록을 보이던 명령(`close_probe.py`). 이유: 결론이 났다(#264의 2026-10-20·10-21 측정, #317, [docs/operations.md](docs/operations.md#하루-한-번-예측-264)). 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 필요 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에, … 배포 뒤 첫 거래일 밤에 돌려 #264에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-dividends` | **삭제됨** (2026-11-06, #567): KIS 수정주가와 원주가 KRX 일봉의 종가 비가 바뀌는 날과 락 구분 표시를 세던 명령(`dividend_probe.py`). 이유: 결론이 났다(#310, 2026-10-21 밤 `probe-dividends-310`, [docs/operations.md](docs/operations.md#배당과-수정주가-확인-310)). 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 필요 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에 돌려 #310에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-nav` | 단일종목 ±2배 ETF 16종(`products.PRODUCTS`)의 순자산을 KIS 어디서 받을 수 있는지 잰다(#305): 상품마다 ETF/ETN 현재가(`FHPST02400000`)의 순자산 총액, NAV 비교추이 일별(`FHPST02440200`, 상장 전부터 거슬러)·종목(`FHPST02440000`)·분별(`FHPST02440100`)을 묻고, 답과 필드 모양, 값이 있는 상품 수, 배율·상장일이 상품 목록과 같은지, 일별이 상장일(2026-05-27)까지 가는지와 빠진 거래일, 그날 값이 물은 시각에 들어와 있는지, 순자산 총액이 상장 주수 × NAV와 어느 단위에서 같은지를 출력한다. **시세 값은 출력하지 않는다.** 100번 남짓, 0.5초 간격, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-nav-305`가 2026-10-21 밤에 돌려 #305에 남겼다([docs/operations.md](docs/operations.md#etf-순자산-출처-확인-305)). 쓸 수 있는 답을 하나도 못 받으면 종료 코드 1 | 필요 **바뀜** (2026-11-04, #506): "배포 뒤 첫 거래일 밤에 돌려 #305에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-krx-etf` | KRX Open API의 ETF 일별매매정보(`etf_bydd_trd`)가 단일종목 ±2배 ETF 16종에 무엇을 주는지 잰다(#305): 상장 전 거래일, 상장일(2026-05-27), 그 뒤 주마다 첫 거래일, 마지막 5거래일을 묻고, 날마다의 답(401·403·429는 KRX FAQ의 뜻과 함께), 필드와 모양(서비스 페이지의 19개와 견줘), 종목코드가 든 필드, 상품마다 상장일부터 행이 있는지, 물은 때 마지막 거래일과 그 전 거래일이 들어와 있는지, 상장좌수(`LIST_SHRS`)가 날마다 바뀌는지와 시가총액 ÷ 종가와 같은지, 순자산총액이 상장좌수 × NAV와 어느 단위에서 같은지, 같은 날의 KIS NAV(`FHPST02440200`)와 순자산 총액·주수(`FHPST02400000`)와 같은지를 출력한다. **시세 값은 출력하지 않는다**(KRX 약관: 제3자 제공 금지). `KRX_OPENAPI_KEY`가 있어야 하고 헤더로만 보낸다. KRX 30번쯤 1초 간격, KIS 60번쯤 0.5초 간격(캐시된 토큰)이라 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-krx-etf-305`가 키를 넣은 그 밤(2026-10-23)에 돌려 #305에 남겼다([docs/krx-openapi.md](docs/krx-openapi.md), [docs/operations.md](docs/operations.md#krx-open-api-etf-일별매매정보-확인-305)). 행이 있는 답을 하루도 받지 못하면 종료 코드 1 | 없어도 된다(8절만 건너뛴다) **바뀜** (2026-11-04, #506): "키를 넣은 뒤 첫 밤에 돌려 #305에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `check-fallbacks` | 한도로 잘린 구독을 대신하는 REST 조회를 하나씩 한 번 부르고, 기록할 이벤트나 실패 이유를 출력한다. 기록하지 않고 REST만 써서 수집기가 도는 중에 실행해도 된다. 실패가 있으면 종료 코드 1([docs/kis-feeds.md](docs/kis-feeds.md#rest-대체-조회)) | 필요 |
| `backfill` | 빠진 1분봉을 한 번 채운다. 수집기를 멈추고 실행한다 | 필요 |
| `archive [--recheck]` | 녹화한 날을 합쳐 `KRX_OBJECT_STORE`에 올린다. `--recheck`는 모든 파일을 저장소와 비교한다. 그다음 30일 지난 `raw` 파티션을, 저장소에서 받은 사본의 SHA-256이 같으면 서버에서 지우고 지운 양과 남은 양을 출력한다(#607, [docs/operations.md](docs/operations.md#서버의-raw-30일-607)). **바뀜** (2026-11-06, #613): 올리기만 했다 → 30일 지난 raw도 지운다. 이유: 서버 데이터의 대부분이 raw이고 날마다 는다. raw는 S3에 영구 보관된다(사용자 결정 2026-11-06, #607) | 필요(쓰지는 않는다) |
| `backup-state` | 서버 상태(`state/`의 `engine.sqlite3`, `trader.sqlite3`, `prevalidations/`, `scheduled.json`, `models/`, `experiments/`의 허용 목록 실험 리포트(#604))를 지금 한 번 `KRX_OBJECT_STORE`의 `state-backup/state.tar.gz`에 올린다(#576). 수집기가 매일 05:35에 하는 것과 같다. `fill_notices.sqlite3`는 넣지 않는다([docs/operations.md](docs/operations.md#서버-상태-백업과-복원-576)) | 없어도 된다 |
| `restore-state [--from 파일] [--replace] [--if-backed-up]` | 가장 최근 서버 상태 백업(또는 `--from`의 `state.tar.gz`)을 임시 폴더에 받아 검사하고, 통과하면 `state/`에 되살린다(#576). 엔진이나 trader가 돌고 있으면 아무것도 바꾸지 않고 거부한다(복원하는 동안 시작한 둘은 끝날 때까지 기다린다). 있는 파일은 바꾸지 않고 거부하고, `--replace`면 `state/before-restore-<시각>/`로 옮긴다(지우지 않는다). `--if-backed-up`은 저장소에 백업이 없으면 아무것도 하지 않고 0으로 끝난다(새 서버의 `deploy/setup-server.sh`가 쓴다). 엔진과 trader를 멈추고 돌린다 | 없어도 된다 |
| `restore-drill` | 복원 시험(#576, 예약 작업 `restore-drill-577`): 가장 최근 서버 상태 백업을 임시 폴더에 받아 SHA-256, `PRAGMA integrity_check`, 표마다 행 수를 백업 때와 견주고, 실험 리포트를 JSON으로 읽어 보고(#604) 개수와 ok·fail을 출력한다. 상태 폴더는 바꾸지 않는다. 실패하면 종료 코드 1 | 없어도 된다 |
| `replay <YYYY-MM-DD> [--db 경로]` | 녹화한 하루를 엔진으로 다시 돌려 별도 DB에 쓴다. 요약은 도달 정답으로 센 채점 수다(#170, 전에는 기준 A) | 없어도 된다 |
| `smt [--day YYYY-MM-DD]` | 그날(기본: 오늘)의 SMT 다이버전스(#520, [docs/design.md](docs/design.md) 5장 "SMT 다이버전스")를 다시 계산해 센다: 녹화한 날은 엔진의 시장 보기로 재생해서, 아니면 채운 1분봉으로. 쌍, 방향, L, 넘은 쪽, 출처(`live`, `reconstructed`)별 건수를 출력하고, 그날 엔진이 장중에 남긴 사건(`live`)이 있으면 다시 계산한 것과 같은지, 다르면 다른 사건의 시각을 출력한다. 가격은 출력하지 않는다. 데이터 폴더와 엔진 DB를 읽기만 한다. 녹화도 채운 1분봉도 `live` 사건도 없으면 종료 코드 1 | 없어도 된다 |
| `probe-sample-keys [--day YYYY-MM-DD]` | 밤 학습이 표본 저장소에 둔 날의 키가 왜 바뀌는지 본다(#565): 평소 밤에 새 날 말고 그 앞 거래일도 다시 만든다. 표본 저장소의 날과 파일(그 밤이 만든 날), 마지막 두 날(기본: 저장소의 마지막 날과 그 앞 날. `--day`로 마지막 날을 고른다)의 키 입력 가운데 그 날의 밤 학습이 시작한 뒤(`KRX_TRAIN_AT`) Object Store에 올라간 것과 지금 SHA-256이 키와 다른 것(Object Store, 학습 캐시, 데이터 폴더의 크기·시각, 합치지 않은 part 수), 마지막 날의 아직 합치지 않은 part 파일(표본이 읽는 파티션과 아닌 것), 지난 실행 뒤 키가 바뀐 날과 바뀐 부분(입력의 바뀜·더함·뺌, 거래 일정의 다른 날, 그 밖의 부분)을 출력한다. 상태 폴더, 데이터 폴더, Object Store(목록과 digest)를 읽고, 쓰는 것은 다음 실행이 견줄 `state/probe-sample-keys.json`뿐이다. 시세 값은 없다(경로, 크기, 시각, 개수, SHA-256 앞 12자만). 표본 저장소에 날이 없거나 `KRX_OBJECT_STORE`가 없으면 종료 코드 1. 서버에서는 예약 작업 `probe-sample-keys-565`, `probe-sample-keys-565-again`이 두 거래일 밤에 돌려 #565에 남긴다([docs/operations.md](docs/operations.md#표본-키-프로브-565)) | 없어도 된다 |
| `bar-close-times [--day YYYY-MM-DD \| --tonight]` | 거래일 하루의 봉 마감이 얼마나 걸렸는지 앞 거래일 5일과 견주어 출력한다(#589): 날마다 봉 마감 수, 봉 마감에서 첫 저장(기준 예측)·최종 합의·마지막 저장까지의 초(중앙값/90번째 백분위/최대), trader의 호가 끊김(5초 넘게) 수(끝나지 않은 끊김은 따로, 지금까지의 초), 알고리즘마다 첫 저장 뒤 그 예측이 저장되기까지의 초와 시간 초과·오류가 난 봉 마감 수, 그날 이벤트 루프를 1초 넘게 쥔 봉 마감(엔진의 `runs` `loop_held`, #589부터), 그중 GC가 겹친 수(#621)와 그 가장 큰 단계. 엔진 DB의 예측이 저장된 시각(`displayed_at`)으로 잰다. **시세 값은 출력하지 않는다**(날짜, 개수, 초). 엔진 DB와 `state/trader.sqlite3`를 읽기만 해 수집기가 도는 중에 실행해도 된다. 기본 날은 오늘까지 예측이 있는 마지막 날이다. `--tonight`은 실행하는 밤의 날(날짜, 06:00 전이면 그 앞날)이고, 예약 작업이 이것으로 돌린다. 준 날(`--day`, `--tonight`)에 예측이 없거나 예측이 하나도 없으면 종료 코드 1. 서버에서는 예약 작업 `bar-close-times-589`가 배포 뒤 첫 거래일 밤에 돌려 #589에 남긴다([docs/operations.md](docs/operations.md#봉-마감의-이벤트-루프-막힘-538)) | 없어도 된다 |
| `session-memory [--day YYYY-MM-DD \| --tonight]` | 장이 끝난 뒤 엔진의 메모리를 출력한다(#623): 그날 엔진이 쉴 때마다(마지막 정답 뒤, 하루 점검 뒤) 놓은 것과 남긴 것의 개수, 쉬기 전과 뒤의 엔진 RSS, 할당자가 가진 것(엔진 DB의 `runs` `memory_rest`), 15시부터 시간마다 서버의 여유 메모리 최소와 swap 최대, 엔진의 RSS와 스왑 최대(`state/memory-hours.json`), 실행 때 서버의 여유 메모리와 swap. **시세 값은 출력하지 않는다**(시각, 개수, MB, 초). 읽기만 해 수집기가 도는 중에 실행해도 된다. 기본 날은 오늘이다. `--tonight`은 실행하는 밤의 날(날짜, 06:00 전이면 그 앞날)이고, 예약 작업이 이것으로 돌린다. 그날 엔진이 쉰 기록이 없으면 종료 코드 1. 서버에서는 예약 작업 `session-memory-623`이 배포 뒤 첫 거래일 밤에 돌려 #623에 남긴다([docs/operations.md](docs/operations.md#3-매일-일어나는-일)의 "장 마감 뒤 메모리 (#623)") | 없어도 된다 |
| `rescore` | 2026-10-12부터의 지난 예측의 도달 정답(`touches`)을 녹화로 다시 계산해, 다른 행만 바꾼다. 옛 채점(`class_a`·`class_b`)과 특수 표시는 그대로다. 엔진이 돌고 있으면 거부한다(종료 코드 1). 엔진을 멈추고 장 시간 밖에 돌린다. 빠진 정답은 엔진이 시작할 때 저절로 채운다([docs/operations.md](docs/operations.md#지난-예측의-도달-정답-168), #168) | 없어도 된다 |
| `fit <알고리즘> [--days N]` | 최근 날들로 학습해 `state/models/`에 설치한다. 녹화된 날은 녹화 이벤트(중간가), 백필만 있는 날은 1분봉(종가)으로. 다른 종류의 알고리즘(변동폭의 `mandelbrot`, #181, 장벽의 `breiman`, #184, 15·30·60분 장벽의 `breiman-15m` 등, #263)은 그 종류의 정답으로 | 없어도 된다 |
| `prevalidate <알고리즘> [--days N] [--revision]` | 새 알고리즘을 녹화된 최근 20거래일(최소 10일)로 재생해 사전 검증한다. `--revision`은 코드가 바뀌어 수습으로 돌아간, 돌고 있는 4분 알고리즘을 바뀐 뒤의 녹화일로만 다시 판정한다(거부되면 엔진이 은퇴시킨다, #454). 다섯 검사를 넘으면 같은 walk-forward 재생으로 수습 판정과 같은 통계를 재(`promotion`, 수준은 98.3%를 시험 예산 `KRX_PROMOTION_BUDGET`으로 나눈 것) 그 밤 정식으로 올리거나 거부하고, 판정할 수 없으면 실시간 날로 판정하는 수습으로 넣는다(#681; **바뀜** (2026-11-10, #684): 통과하면 엔진이 다음 봉 마감부터 수습으로 돌렸다. 이유: 새 알고리즘은 앞으로 20일을 기다리지 않고 지난 날로 판정한다는 사용자 결정). 거부되면 종료 코드 1([docs/engine-api.md](docs/engine-api.md#사전-검증-24)). 변동폭의 `mandelbrot`(#181)과 장벽의 `breiman`(#184)도 이것으로 검증한다. 하루 한 번 예측 종류의 `cox`(#264)는 녹화 재생 대신 KRX 일봉 10년의 walk-forward로 검증한다(`--days`는 쓰지 않는다. 서버의 예약 작업은 녹화 일수를 기다리지 않고 따로 돈다). 서버에서는 예약 작업이 녹화 10거래일부터 밤마다 하나씩 돌려 결과를 #204에 남긴다(#204). 실행마다 `state/prevalidations/<이름>.history.jsonl`에 한 줄을 남기고, 전에 돈 적이 있으면 그 횟수와 같은 날을 재생한 횟수를 출력한다(#262). 같은 날로 다시 돌리면 통과 기준이 더 엄격하다(거래일 단위 하한, Bonferroni) | 없어도 된다 |
| `screen-queue [--days N] [--candidate <알고리즘> --out <파일>]` | 사전 검증을 기다리는 봉 마감 알고리즘을 값싸게 훑어 밤 사전 검증의 순서만 정한다(#599). 알고리즘마다 따로 프로세스에서(10분, 메모리 1,000 MB, 그 밤 전체 1시간) 사전 검증과 같은 최근 녹화 20거래일을 재생 없이 walk-forward로 예측하고, 그 종류의 기준 예측마다 거래일 단위 lift를 내어 가장 약한 것을 점수로 쓴다. 점수 자리와 먼저 온 자리가 번갈아 오고 세 밤 밀린 것이 맨 앞이다. 아무것도 들이거나 거부하지 않는다: 기다리는 알고리즘은 모두 사전 검증을 받는다. 순서와 점수를 출력하고 `state/prevalidations/queue.json`과 `queue.history.jsonl`에 남긴다. 시세 값은 출력하지 않는다. `--candidate`는 그 알고리즘 하나를 이 프로세스에서 훑어 `--out`에 쓴다(각 후보의 프로세스가 도는 것). 서버에서는 예약 작업 `screen-queue-599`가 사전 검증 앞에 돌려 #204에 남긴다([docs/operations.md](docs/operations.md#새-알고리즘-사전-검증-24)) | 없어도 된다 |
| `neyman-variants` | Neyman 변형 실험(#455): Neyman 그대로(A), 뒤집힘 맞바꿈(B), `slot_return_5d`·`slot_return_20d`를 뺌(C), C + 2차 입력 r = (k + h) ÷ σ̂₄(D)를 같은 봉 마감으로 견준다. 날마다 그 전 20거래일로 Neyman 사전 검증과 같은 표본·설정·시드로 학습한다. 첫 실행은 발견 단계다: Neyman 사전 검증의 날들(2026-10-12–10-30)에서 날마다 짝 이득의 표준편차로 평가할 날 수 N을 정해 `state/experiments/neyman-variants-283.json`에 남긴다(다시 계산하지 않는다). 그 뒤 실행은 2026-11-02 뒤 첫 N개의 녹화일(채점 봉 마감 200개 이상) 창이 차면 돌리기 전에 정한 규칙으로 한 번 판정한다. 차기 전에는 판정하지 않는다. **바뀜** (2026-11-04, #535): 실시간 창은 B, C, D를 98.333%로 판정하고 N은 206이었다 → D − A 하나만 판정한다(lift 0.5%p, 양측 95%, 저장된 발견의 s_d로 N 56). B − A, C − A는 설명이다. 계획은 날짜와 이유와 함께 리포트로 옮기고(`plan`) 발견 단계는 다시 돌리지 않는다. 이유: 206일이면 판정이 2027년 8–9월에 나온다. 세 비교는 백필 날로 판정한다(사용자 결정 2026-11-04, #532). `--backfill`은 그 백필 판정이다: 세 비교를 등록 그대로, 발견 단계가 학습한 첫날보다 앞선 백필 거래일(그 앞에 데이터가 있는 날 40일, 채점 봉 마감 200개 이상)로 한 번 판정해 `state/experiments/neyman-backfill-532.json`에 남긴다. 날짜 목록은 어느 팔도 학습하기 전에 정해 두고 다시 정하지 않는다. `--until <시각>`을 주면 그 시각까지만 날 단위로 걷고 끝낸 날을 `state/experiments/neyman-backfill-532.days.jsonl`에 남겨 다음 실행이 이어 한다(#643). 엔진을 재생하지 않고 학습 표본으로 돈다. 시세 값은 출력하지 않는다. Neyman은 바꾸지 않는다. 서버에서는 예약 작업 `neyman-variants-283`과 `neyman-backfill-532`가 돌려 #455에 남긴다([docs/engine-api.md](docs/engine-api.md#neyman-변형-실험-455), #458, [백필 판정](docs/engine-api.md#neyman-변형의-백필-판정-532)) | 없어도 된다 |
| `train [--day YYYY-MM-DD]` | 밤 학습 한 번. `--day`가 없으면 Object Store에 모든 종목의 백필 1분봉이나 녹화된 체결이 있는 가장 최근 날. **삭제됨** (2026-11-06, #572): `--from-store`(서버가 S3에 올린 설정과 휴장일 달력으로 학습한다, EC2 Spot, #23). 이유: #23 계획 없음, 계정 정책이 Spot을 막음(사용자가 2026-11-05에 정했다). 대신: 서버 안 밤 학습, 필요하면 git 이력. | 없어도 된다 |
| `registry-password` | **삭제됨** (2026-11-06, #572): ECR 레지스트리의 `docker login` 암호를 출력했다. `deploy/push-image.sh`가 Spot 학습용 이미지를 올릴 때 썼다(#23). 이유: #23 계획 없음, 계정 정책이 Spot을 막음(사용자가 2026-11-05에 정했다). 대신: 서버 안 밤 학습, 필요하면 git 이력. | |
| `macro` | 오늘의 매크로 지표를 한 번 읽고 국면을 정해 `data/macro`에 기록한다(#8). 실패한 출처가 있으면 종료 코드 1 | 없어도 된다 |
| `signal-review [--days N] [--from YYYY-MM-DD]` | 텔레그램 신호 기준(`KRX_SIGNAL_MIN_PROBABILITY`, `KRX_SIGNAL_MIN_AGREE`)을 정할 실측을 텍스트로 출력한다(#206): 지금 설정과 엔진이 남긴 신호, 순위표(상승·보합·하락 예측별 적중률), 정식 알고리즘의 정식 뒤 성적, 기준 후보(최종 합의 확률 × 같은 방향 정식 수)마다 엔진의 신호 규칙으로 낸 신호 수와 상승·하락 적중률(95% 구간, 상승은 거래일 단위 구간도), 확률 구간 비교([0.60, 0.70) 대 ≥ 0.70, [0.70, 0.75) 대 ≥ 0.75), 미리 정한 판정 규칙의 갈래. 읽기만 한다. `--days`는 예측이 있는 최근 N일, 기본 20. `--from`이면 그날부터 예측이 있는 첫 N일(고정 창). 서버에서는 예약 작업이 돌려 #206에 남긴다([docs/operations.md](docs/operations.md#신호-기준-검토-206)) | 없어도 된다 |
| `error-holdout` | #431의 리포트에서 고른 turing의 가설 넷(#447에 고정: 조용할 때 진다, 움직일 때 이긴다, 장 초반에 이긴다, 점심에 진다)을 고정 홀드아웃 창(2026-10-30 뒤 turing의 정답 버전 3 예측이 있는 첫 10거래일)으로만 판정해 출력한다: 가설마다 채점·짝·거래일 수, 정답률, `baseline-climate` 대비 lift, 짝 구간과 거래일 구간(본페로니 98.75%), 판정(`섰다`, `서지 않음`, `표본 부족`). σ̂₄ 삼분위 경계는 발견 기간(2026-10-12–10-30)으로 돌 때마다 다시 낸다. 창이 차기 전에는 판정하지 않는다. 가격과 시세 값은 없다. 읽기만 한다. 서버에서는 창이 찬 첫 밤 예약 작업이 한 번 돌려 #447에 남긴다([docs/operations.md](docs/operations.md#홀드아웃-판정-447)) | 없어도 된다 |
| `climate-gate` | #449에 고정한 국면 문(봉 마감이 σ̂₄ 하위 삼분위이거나 12:00·12:30 칸이면 `baseline-climate`의 예측, 아니면 turing의 예측)을 #447과 같은 고정 홀드아웃 창에서 turing, 기후와 같은 봉 마감으로 비교해 출력한다: 세 팔의 세 분류 맞힘률, 문의 turing 대비·기후 대비 lift와 짝 구간·거래일 95% 구간, 기후로 넘긴 비율, 판정(turing 대비 거래일 하한 > 0일 때만 `섰다`). σ̂₄ 경계는 #447처럼 발견 기간(2026-10-12–10-30)으로 돌 때마다 다시 낸다. 창이 차기 전에는 판정하지 않는다. 엔진에는 넣지 않는다. 가격과 시세 값은 없다. 읽기만 한다. 서버에서는 `error-holdout-447`과 같은 밤 예약 작업이 한 번 돌려 #449에 남긴다([docs/operations.md](docs/operations.md#국면-문-홀드아웃-449)) | 없어도 된다 |
| `smt-experiment [--holdout]` | SMT 다이버전스(#520)가 4분 예측의 맞힘률을 올리는지 재는 실험(#523, 스펙 #519). 고정 규칙(사건에서 하락 SMT면 하락, 상승 SMT면 상승을 그 쌍의 주식마다)과 엔진이 그 봉 마감에 낸 최종 합의(`consensus-final`)를 같은 (주식, 봉 마감)에서 견준다. 인자가 없으면 탐색이다: 2026-11-04까지의 녹화일로 세 쌍의 1분 수익률 상관과 함께 있는 시간, 쌍·방향·L·넘은 쪽별 사건 수와 사건이 있은 날, 사건 뒤 상승·보합·하락, 규칙 − 엔진 맞힘률(%p, 짝 구간과 거래일 구간), 규칙 − `baseline-climate`을 출력하고, 채운 1분봉만 있는 날(정답이 근사)은 따로 적는다(엔진과 견주지 않는다). `--holdout`은 사전 등록 판정이다: 2026-11-04 뒤 자격이 있는 첫 10거래일에서 판정 넷(하락·상승 × L 15·30)마다 규칙 − 엔진의 거래일 98.75% t 구간(본페로니)으로 `섰다`, `서지 않음`, `표본 부족`을 낸다. 창이 차기 전에는 판정하지 않는다. 사전 등록 내용은 [docs/operations.md](docs/operations.md#smt-실험-523)에 있다. 가격은 없다(건수, 비율, %p, 날짜만). 데이터 폴더와 엔진 DB를 읽기만 한다. 서버에서는 예약 작업 `smt-discovery-519`(배포 뒤 첫 밤)와 `smt-holdout-519`(창이 찬 첫 밤)가 한 번씩 돌려 #519에 남긴다 | 없어도 된다 |
| `nightly-hit-rates [--nights N]` | 밤 학습의 교체 리포트가 log loss 옆에 남긴 맞힘률(#437)을 최근 N밤(승격 목록에 맞힘률이 있는 밤, 기본 10)에 걸쳐 알고리즘별로 요약해 출력한다: log loss로 승격한 밤과 그중 맞힘률이 떨어진 밤(불일치), "맞힘률 우선" 규칙이었다면 승격한 밤, 두 규칙이 갈린 밤, 평균 맞힘률·방향 예측 비율·차이(기준가별). 개수, 비율, 날짜만 나온다. `KRX_OBJECT_STORE`의 `promotions/`만 읽는다. 서버에서는 예약 작업 `nightly-hit-rates-437`이 10밤이 쌓이면 한 번 돌려 #437에 남긴다([docs/operations.md](docs/operations.md#밤-학습과-모델-교체-22)) | 없어도 된다 |
| `probe-night [--day YYYY-MM-DD]` | 녹화한 KOSPI200 야간선물의 한 밤(그날 18:00 – 다음 날 06:00, #264)을 요약해 출력한다: KIS의 구독 응답, 체결(`H0MFCNT0`)·호가(`H0MFASP0`)의 프레임·레코드 수, 레코드당 필드 수, 첫·마지막 수신과 시간대별 수, 시각 필드의 원문, events 행과 정규화하지 못한 레코드, 결측 구간, KIS 마스터의 KOSPI200 선물 코드와 야간선물 마스터 앞 10줄. **시세 값은 출력하지 않는다**. 녹화 파일과 공개 마스터 파일만 읽어 수집기가 도는 중에 실행해도 된다. 30일 지나 서버에서 지운 raw는 `KRX_OBJECT_STORE`에서 받아 읽는다(#607). 기본은 18:00 전부터 구독한 가장 최근의 끝난 밤이다. 서버에서는 예약 작업 `probe-night-264`가 첫 밤 뒤(2026-10-20)에 돌려 #264에 남겼다([docs/operations.md](docs/operations.md#야간선물-녹화-확인-264)) | 없어도 된다 **바뀜** (2026-11-04, #506): "첫 밤 뒤에 돌려 #264에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `probe-expected-open [--days N]` | **삭제됨** (2026-11-06, #567): 녹화한 raw 통합 호가에서 단일가의 예상 체결(#304)을 시간대·거래소 구분 코드별로 세고 KRX 일봉 시가·종가와의 틱 차이를 보이던 명령(`expected_open_probe.py`). 이유: 결론이 났다(#314, #341, [docs/operations.md](docs/operations.md#예상-체결-녹화-확인-304)). 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 없어도 된다 |
| `probe-gaps` | 녹화한 모든 날의 WebSocket 결측과 종가 단일가 구간을 센다(#344): 날마다 `gaps`의 구간을 이유·시작 시각·길이·피드·키로(한 끊김이 구독마다 여는 구간은 한 줄로 묶어 피드·키 수와 길이의 범위로), 거래일마다 마감 10분 전부터 마감 1분 뒤까지(보통 15:20–15:31) WebSocket 피드별 마지막 프레임 시각과 수, 모든 프레임 사이의 가장 긴 침묵, 겹친 결측, KRX(`H0STCNT0`)·통합(`H0UNCNT0`) 체결의 종목마다 마감 뒤 1분 안의 첫 체결(시각과 원문 코드, 종가 단일가 체결의 표시가 문서에 없어 이것으로 본다), `bars`의 1분봉 채움. **시세 값은 출력하지 않는다**. 녹화 파일만 읽어 수집기가 도는 중에 실행해도 된다. 30일 지나 서버에서 지운 raw는 `KRX_OBJECT_STORE`에서 하루씩 받아 읽는다(#607). 서버에서는 예약 작업 `probe-gaps-344`가 2026-10-23 밤에 돌려 #344에 남겼다([docs/operations.md](docs/operations.md#websocket-결측-확인-344)) | 없어도 된다 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에 돌려 #344에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `recording-quality [--day YYYY-MM-DD]` | 거래일 하루의 기록 품질을 출력한다(#570): 장중(보통 09:00–15:30) WebSocket 끊김을 한 끊김에 한 줄로, 길이별(5초 미만, 5–60초, 60초 이상) 수와 피드별 끊김 횟수·끊긴 초·stale 구간, 녹화한 체결로 엔진처럼 만든 1분봉과 KIS 백필 1분봉(`FHKST03010230`, 통합 `UN`은 `H0UNCNT0`, KRX `J`는 `H0STCNT0`)의 연속매매 분마다 견줌(빠진 분, KIS에 없는 분, 거래량이 모자라거나 넘는 분, 종가가 다른 분, 각각 결측 구간 밖의 수, 두 번 기록됐을 수 있는 행, 녹화 거래량 ÷ KIS), 그날 raw의 해석하지 못한 프레임과 필드가 모자라거나 남는 프레임(피드별), 엔진 `/v1/status`의 REST 조회 실패(엔진의 오늘이 그날일 때만), 지난밤 야간선물 체결·호가의 레코드와 정규화하지 못한 것을 앞선 밤들의 중앙값과 견준 것, 실행 시간과 최대 메모리. 둘째 줄 `에이전트 차례:`가 기준을 넘은 까닭을 말한다. **시세 값은 출력하지 않는다**. 녹화 파일과 엔진 API만 읽어 수집기가 도는 중에 실행해도 된다. 30일 지나 서버에서 지운 raw는 `KRX_OBJECT_STORE`에서 받아 읽는다(#607). 기본 날은 그날 20:30(1분봉 확정) 뒤면 그날, 아니면 앞 거래일이다. 서버에서는 예약 작업 `recording-quality-570`이 거래일 밤마다 돌려 #571에 남긴다([docs/operations.md](docs/operations.md#기록-품질-리포트-570)) | 없어도 된다 |
| `night-book-bytes [--verdict]` | KOSPI200 야간선물 호가(`H0MFASP0`)가 밤마다 차지하는 바이트를 잰다(#605): 2026-11-06 저녁의 밤부터 거래일 밤마다 raw와 parsed(`events`) 층의 합친 Parquet 바이트(그 밤의 행 비율만큼), 행 수, 18:00–06:00의 가장 긴 침묵, 완전한 밤인지(10분 넘는 침묵이 없고, 다 합쳐졌고, 다 읽었다), 완전한 밤 7개의 중앙값과 기준 100 MiB. `--verdict`는 2026-11-20의 규칙(중앙값이 기준을 넘고 기록·리포트 밖의 모듈이 이 호가를 이름으로 부르지 않으면 구독을 멈춘다, 아니면 계속 기록한다)을 적용한 결론과 할 일, 이 호가를 부르는 모듈을 더한다. **시세 값은 출력하지 않는다**. 녹화 파일과 패키지 소스만 읽고 `state/night-book-605.json`만 쓴다. 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `night-book-bytes-605`와 `night-book-verdict-605`가 돌려 #605에 남긴다([docs/operations.md](docs/operations.md#야간-호가-기록량과-판정-605)) | 없어도 된다 |
| `inverse-replay [--day YYYY-MM-DD]` | 005930의 명시적인 아래 호출(방향의 `down`, 장벽의 `down_first`)을 녹화한 인버스 ETF(0193L0) 호가로 재생해 출력한다(#606): 표시된 뒤 첫 매도 1호가에 1주 사고 005930의 장벽이나 만기에 매수 1호가에 판다. 끊긴 구간과 5초보다 오래된 호가는 빼고, 수수료를 뺀 원 단위 순손익을 날마다, 종류별, 칸별로 보인다(최근 20거래일). 매매하지 않고 읽기만 한다. 가격은 없다. 예약 작업 `inverse-replay-606`이 거래일 밤마다 돌린다([docs/operations.md](docs/operations.md#인버스-etf-재생-606)) | 없어도 된다 |
| `probe-member-frames` | **삭제됨** (2026-11-06, #567): 녹화한 회원사(`H0UNMBC0`) raw 프레임 중 필드 수가 레코드 수 × 78이 아닌 것을 세던 명령(`member_frames_probe.py`). 이유: 결론이 났다(#381, [docs/operations.md](docs/operations.md#회원사-프레임의-필드-수-확인-381)). 같은 일을 #419 뒤 수집기의 WARNING과 그날 수(`/v1/status`의 `kis.feeds[].short_frames`)가 한다. 사용자가 2026-11-05에 지우기로 정했다(#563, #505의 "명령은 남긴다"를 바꿨다). 대신: git 이력. | 없어도 된다 **바뀜** (2026-11-04, #506): "배포 뒤 첫 밤에 돌려 #381에 남긴다" → 남겼다. 그 예약 작업은 `JOBS`에서 빠졌고, 서버에서도 손으로 돌린다. 이유: 할 일을 마쳤다. |
| `simulate daily`, `simulate bar-close` | 수습 시뮬레이션(#298). `daily`: 하루 한 번 예측 종류(`cox`)의 수습이 시뮬레이션을 기다리면 최근 20거래일을 그 알고리즘이 수습으로 돌았다고 치고 재생하고(날마다 그 전날까지로 다시 학습), 20일째 판정을 바로 한다. 통과하면 정식, 못 하면 수습이 이어진다(다음 판정 40일째). 사전 검증을 통과했지만 엔진이 아직 올리지 않은 알고리즘은 수습에 올린다. 그 종류의 최근 정답이 다 들어오지 않았거나(`open_to_close`: 애프터마켓부터의 종가 단일가 가격) 사전 검증이 지금 코드·정답 버전·입력의 것이 아닌 수습은 시뮬레이션하지 않고 기다린다(`<이름>: its simulation waits - …`, #264, #317. #315의 판정 보류는 #317에서 없어졌다). 시뮬레이션 예측은 실시간 예측과 따로 남고 순위표·통계에는 들지 않는다. 엔진이 도는 중에도 된다. `bar-close`: 4분 종류(방향, 변동폭, 장벽)의 수습이 시뮬레이션을 기다리면 녹화된 최근 거래일(10–20일, 10일보다 적으면 하지 않는다)을 첫날 전까지로 한 번 학습해 재생하고 바로 판정한다(몇 시간 걸릴 수 있다). 서버에서는 예약 작업 `simulate-daily-298`(사전 검증을 통과한 그 밤, #298과 #264)과 `simulate-bar-close-298`(녹화 10거래일부터, #298과 #204)이 돌린다([docs/engine-api.md](docs/engine-api.md#상태-관리-17)) | 없어도 된다 |
| `rollback <알고리즘>` | 직전 모델로 되돌린다. 도달 정답에서는 그 정답으로 학습한 모델로만(#193) | 없어도 된다 |
| `retire <알고리즘>`, `unretire <알고리즘>` | 은퇴시킨다, 은퇴를 풀어 수습으로 되돌린다 | 없어도 된다 |
| `test-alert` | 텔레그램에 `[테스트]` 메시지를 보낸다 | 필요(쓰지는 않는다) |
| `notify <글> [--log-only]` | 텔레그램에 `[알림] <글>`을 보낸다. `--log-only`면 보내지 않고 INFO 줄(`notice, logged only: <글>`)로 로그에만 남긴다(#614: 깨끗한 배포의 보고. 텔레그램에는 08:40 준비 줄이 커밋을 적는다). `deploy/auto-update.sh`가 쓴다 | 없어도 된다 |
| `fake-kis [--port 18080] [--host 127.0.0.1] [--trades]` | 가짜 KIS 서버(아래 "키 없이 확인하기") | 없어도 된다 |
| `demo-engine [--port 8090] [--host 127.0.0.1] [--db state/demo.sqlite3] [--pace 초]` | 데모 엔진 API(아래 "화면을 만들 때"). `--pace`는 10:00부터 이어 재생하며 스트림을 보낸다 | 없어도 된다 |

> **삭제됨** (2026-11-09, #667): #505가 손으로 돌릴 때를 위해 남긴 리서치 명령 일곱과 그 모듈, 테스트. `diagnose [--days N]`(남은 결정에 필요한 실측, #82, `diagnose.py`), `daily-check [--days N]`(하루 한 번 예측 종류의 점검, #264, `daily_check.py`), `structure-check [--days N]`(리서치의 시장 구조 주장 실측, #270, `structure_check.py`), `error-breakdown [--days N] [--algorithm NAME]`(알고리즘이 어디서 틀리는지, #431, `error_breakdown.py`), `climate-prior [--days N]`(Shannon의 기후 사전 확률 실험, #429, `climate_prior.py`), `check-closing-flow`와 `check-krx-net-assets`(KRX 마감 흐름이 쌓은 것의 확인, #305, `closing_flow_check.py`). 이유: 사용자 결정(2026-11-09): 이슈가 모두 닫혔고 14일 동안 쓰지 않음. 대신: 결과는 각 이슈의 댓글(#203, #187, #264, #270, #431, #429, #305)과 docs의 실측 기록에 있다. 다른 코드가 쓰던 함수는 #663에서 주인 모듈로 옮겼다(`base_rates`, `holdout`, `stats` 등). 진입 지연(`entry_delay.py`)은 `inverse-replay`와 trader의 재생이 쓰므로 남기고, `diagnose` 10절이 쓰던 `entry_delays`만 지웠다. 진단만 쓰던 `macro.decided_before`도 지웠다.

> **삭제됨** (2026-11-09, #653): 결론이 나서 다시 쓰지 않는 측정과 probe의 명령 셋. `sample-profile [--day YYYY-MM-DD] [--algorithms 이름 …] [--limit 분] [--top N]`(녹화한 하루의 표본을 밤 학습처럼 처음부터 만들며 단계와 함수마다 시간을 재던 명령, #512)과 그 예약 작업 `sample-profile-512`, `probe-inverse-book [--day YYYY-MM-DD]`(인버스 ETF KRX 호가 `H0STASP0`의 녹화한 하루를 요약하던 probe, #606), `probe-market-halt --day YYYY-MM-DD [--from HH:MM] [--until HH:MM]`(녹화한 하루의 한 구간에서 시장 전체 매매 정지가 피드에 어떻게 보였는지 요약하던 probe, #625)과 그 예약 작업 `probe-market-halt-625`. 이유: 셋 다 한 번 돌아 결과가 그 이슈에 있고 그 결과를 쓴 일이 머지됐다(`sample-profile`은 2026-11-07 밤, 고친 것은 #630의 #635. `probe-inverse-book`은 2026-11-06 밤, 쓴 것은 #634. `probe-market-halt`는 2026-11-07 밤, 쓴 것은 #632와 #636, #625는 11-08에 닫힘). 결론 난 실험과 probe는 지운다(사용자 결정 2026-11-05, #559, #563과 같은 방식). 대신: 결과는 #512, #606, #625의 댓글에 있다. 인버스 ETF 호가는 `inverse-replay`가 읽는다. 셋 다 상태 폴더에 리포트를 쓰지 않았으므로 서버에 남는 파일은 없다.

> **삭제됨** (2026-11-09, #642): 정답 순서 실험의 명령 `normal-order [--days N]`(공통 정규분포 함수를 쓰는 다섯(Newton, Gauss, Granger, Markov, Kalman)을 위·아래 목표를 독립으로 본 지금 그대로와 정답의 순서로 견주던 실험, #529). 그 예약 작업 `normal-order-529`도 지웠다. 이유: 결론이 났다(다섯 모두 후보 아님, #529 2026-11-08 밤 결과). 대신: 지금의 독립 근사를 그대로 쓴다. 리포트 `state/experiments/normal-order-529.json`은 서버에 기록으로 남는다. 결과는 #529의 댓글에 있다.

> **삭제됨** (2026-11-05, #566): 결론이 난 실험 넷의 명령. `measure-passive-entry [--engine-url 주소]`(대기 매수를 녹화로 재던 측정, #432), `cost-map-audit [--engine-url 주소]`(비용 지도 감사, #438), `poisson-link [--days N]`(Poisson의 비대칭 확률 연결 실험, #439), `turing-prior [--days N]`(Turing의 종목과 기후 사전 확률 실험, #448). 그 예약 작업은 #506에서 `JOBS`에서 뺐고, 명령은 손으로 돌릴 수 있게 남겼었다(#505). 이유: 결론이 났다(#432, #438, #439, #448). 14일 동안 손으로 돌린 기록이 없어 사용자가 2026-11-05에 코드와 함께 지우기로 정했다(#559). 대신: git 이력. 결과는 각 이슈의 댓글에 있다.

> **삭제됨** (2026-10-23, #360): `logs-bundle [--days N | --since 때] [--grep 패턴]`(상태 폴더의 로그 파일 `state/logs/`를 가려서 한 파일로 묶던 명령, #356). 이유: 사용자가 로그를 Grafana Cloud Loki로만 남기기로 정했다(2026-10-23). 대신: Loki에서 질의한다([docs/agents/server-logs.md](docs/agents/server-logs.md)).

종료 코드: 0은 성공이다. 2는 설정 오류(`configuration error: ...`)나 잘못된 인자다. 1은 KIS 오류(`KIS error: ...`, Docker가 다시 띄운다)이거나 명령이 할 일을 못 한 경우다. 예: 업로드에 실패한 파티션, 학습할 1분봉 없음, 되돌릴 모델 없음, 텔레그램 전송 실패, 사전 검증 거부, 실패한 REST 대체 조회, 답을 하나도 받지 못한 `probe-supply`. 처리 함수가 없는 명령도 1이다(#499, 아무것도 하지 않고 0으로 끝나지 않는다).

## 문서

- [docs/design.md](docs/design.md): 설계 (목표, 구조와 모듈 지도, 데이터, 알고리즘, 학습, 인프라, 개발 단계). 바뀐 결정은 각 절의 **바뀜**·**삭제됨** 표시에 있다
- [docs/kis-feeds.md](docs/kis-feeds.md): KIS 피드, 키, 한도, 최근월물 찾기
- [docs/toss-feeds.md](docs/toss-feeds.md): 토스증권 Open API(일별 수급의 보조 출처, 외국인 보유의 주 출처(#226), 종목 패널의 회사 이름과 로고(#250)), 토큰 규칙, 허용 IP(#235), 받은 값을 서버 밖으로 내보내지 않는 규칙
- [docs/krx-openapi.md](docs/krx-openapi.md): KRX Open API(단일종목 ETF의 지난날 순자산과 상장좌수의 출처, #305): 2026-10-23 서버 실측, 엔진이 쓰는 필드(A), 백필과 매일 수집, 인증키, 한도, 받은 값을 서버 밖으로 내보내지 않는 규칙
- [docs/events.md](docs/events.md): 녹화 데이터와 1분봉 형식
- [docs/engine-api.md](docs/engine-api.md): 엔진 API, 예측·채점 규칙, 저장 구조, 알고리즘 규약과 학습, 재생
- [docs/operations.md](docs/operations.md): 서버 개통, 설정 값 전체, 매일 일정, 밤 학습과 모델 교체, 자동 업데이트, 키 교체, 장애 대응과 알림, 저장 위치
- [docs/paid-data.md](docs/paid-data.md): 유료 데이터 조사
- [research/](research/README.md): 결정 전의 조사 자료. 단기 매매(스캘핑·데이·스윙) 퀀트 기법, 퀀트 회사, 근거 논문 조사와 그 보강 조사(둘 다 2026-10-17), 거기서 나온 제안 이슈 #262–#267

## 진행 상황

2026-10-11 기준이다. 이슈는 `gh issue list --state all`로 본다. 자세한 것은 [docs/design.md](docs/design.md) 11장에 있다.

| 단계 | 내용 | 상태 |
|---|---|---|
| P0 | 저장소 준비, 설계 문서 | 완료 |
| P1 | 수집기 | 거의 완료(#2–#6, #8, #30). 남은 것: 첫 거래일 확인(#7), ETF 장중 확인(#29), REST 대체 장중 확인(#28, `check-fallbacks`) |
| P2 | 예측·채점 핵심 | 완료: #9–#19, Markov 옵션 IV 국면(#53). 호가 피처를 쓰는 Turing은 녹화가 쌓인 뒤(#55) |
| P3 | Fincept 화면, 빌드 | 구현 완료: 첫 화면과 개인 빌드(#20), 차트·예측 패널·순위표·매크로 줄·엔진 상태·신호 알림·스트림(#21, `krx.2`), 알고리즘 상세 통계 창(#68, `krx.3`), 버튼 한 번으로 업데이트(#92), 자동 개인 빌드(#93, #98). 마지막 개인 빌드는 `custom-v4.5.0-krx.7`이다(`krx.4`–`krx.6`은 Windows 빌드 실패로 Release 없음, #101) |
| P4 | 밤 학습 | 완료. 밤 학습은 상시 서버에서 돈다: 상시 서버의 밤 학습과 08:00 교체(PR #45), 녹화 이벤트로 학습하기(#22, PR #48). **삭제됨** (2026-11-06, #572): EC2 Spot 학습과 비용 상한(PR #67, `deploy/training.yaml`, Cost Guard)과 그 절차(docs/operations.md 3장). 계정의 서비스 제어 정책이 Spot을 막아 2026-10-14부터 보류였다(docs/design.md 8장). 이유: #23 계획 없음, 계정 정책이 Spot을 막음(사용자가 2026-11-05에 정했다). 대신: 서버 안 밤 학습, 필요하면 git 이력. |
| P5 | 호가 알고리즘, 재생 테스트 | 구현 완료: 하루 재생(`replay`), 새 알고리즘 사전 검증(`prevalidate`, #24), Kalman(#25), Shannon(#26), Poisson(#27). 셋은 녹화 10거래일 뒤 사전 검증을 통과해야 실시간에서 돈다 |

## 비밀값

API 키와 토큰은 서버의 `.env`에만 둔다. 저장소에 커밋하지 않는다.
