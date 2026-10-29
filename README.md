# krx-quant

KRX 종목의 4분 뒤 가격 움직임을 여러 퀀트 알고리즘이 예측하고, 예측을 자동 채점해 통계로 보여주는 개인용 엔진이다. 결과는 fork한 Fincept Terminal([codeyoma/FinceptTerminal](https://github.com/codeyoma/FinceptTerminal), `custom` 브랜치)의 화면에서 본다.

엔진(수집기)에는 주문 코드가 없다. 신호만 낸다. 자동 매매(#393)는 따로 도는 trader 프로세스(`krx-quant trade`, compose 서비스 `trader`)가 한다. 지금은 모의(`paper`)뿐이다: 수집기가 내보내는 실시간 호가로 체결을 흉내 내고, 주문은 내지 않는다(설계 [14장](docs/design.md#14-자동-매매-393)). KIS 주문 코드는 trader의 KIS 주문 모듈(`src/krx_quant/kis_orders.py`) 하나에만 있고, 그 밖에서는 `scripts/check_forbidden.py`가 계속 막는다. 지금 그것을 쓰는 것은 KIS 모의투자 서버에 주문해 응답을 재는 프로브(`krx-quant probe-kis-orders`, #403)뿐이다. trader는 아직 쓰지 않는다(#404, #405).

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
uv run python scripts/check_forbidden.py && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
```

테스트 하나가 시간 한도(`pyproject.toml`의 `timeout`, 30초)를 넘으면 `pytest`는 모든 스레드의 스택을 출력하고 그 자리에서 실행 전체를 끝낸다(`timeout_method = "thread"`, #335). 그 뒤의 테스트는 돌지 않는다. 더 오래 걸려야 하는 테스트는 `@pytest.mark.timeout(N)`과 이유를 단다. CI의 테스트 job은 60분을 넘으면 멈춘다(`timeout-minutes`, 평소 11–22분).

`scripts/check_forbidden.py`는 KIS 주문 TR 코드와 주문 엔드포인트(trader의 KIS 주문 모듈 `src/krx_quant/kis_orders.py` 밖에서, `forbidden.ALLOWED_IN`), 비밀값 패턴(`RULES`)과 바뀜·삭제됨 노트에 남은 PR 번호 자리표시(`REPOSITORY_RULES`, #321)가 저장소에 들어오면 실패한다. CI도 같은 검사를 한다. 패턴은 `src/krx_quant/forbidden.py`에 있다. 예약 작업이 GitHub 이슈에 남기는 댓글에서는 `RULES`만 가린다(#215).

> **바뀜** (2026-10-29, #410): KIS 주문 TR 코드와 주문 엔드포인트는 어느 파일에서도 실패했다 → `src/krx_quant/kis_orders.py` 하나에서만 풀린다(그 파일에서도 다른 규칙은 그대로, 토스 주문 규칙도 그대로). 게시물 가림(`redaction.py`)은 그 파일의 TR 코드도 가린다. 이유: 자동 매매(#393)의 주문 코드를 한 곳에 두고, 다른 파일에 주문 코드가 새면 계속 막으려고(#403).

> **바뀜** (2026-10-20, #328): 검사는 주문 코드와 비밀값만 봤다 → PR 번호 자리표시도 본다(저장소 검사에만). 이유: PR을 열기 전에 쓴 자리표시가 남은 채 리뷰에 온 PR이 일곱 개였다(#321).

> **바뀜** (2026-10-24, #375): 자리표시는 `#` 뒤의 `PR`로 시작하는 대문자 이름, `NNN`, `XXX`, `TBD`, 밑줄로 감싼 `PR`이었다 → 대문자로 시작해 `PR`로 끝나는 이름도 자리표시다(`src/krx_quant/forbidden.py`의 주석에 예가 있다). 이유: #375가 그런 자리표시를 단 채 리뷰에 왔고 검사가 잡지 못했다(Codex 리뷰).

PR에는 `pr-refs` 검사(`.github/workflows/pr-refs.yml`, `scripts/check_pr_refs.py`)가 따로 돈다. 본문에서 "Refs" 뒤에 적은 이슈를 머지가 닫으면 실패하고, 어느 이슈가 어떤 구절 때문에 닫히는지 출력한다. 닫힌다고 보는 경우는 둘이다: GitHub GraphQL API의 `closingIssuesReferences`에 그 이슈가 있거나, 본문에 닫는 키워드(close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved)와 그 이슈 번호가 이어 붙어 있을 때다. GitHub는 백틱 안이나 "not" 뒤의 키워드도 읽어서, #340과 #341의 머지가 예약 작업이 아직 댓글을 다는 #310과 #314를 닫았다. 본문을 고치면 검사가 다시 돈다. PR의 Development 사이드바에서 이슈를 잇거나 끊는 것은 검사를 다시 돌리지 않는다(GitHub의 `pull_request` 이벤트에 그런 종류가 없다). 그런 링크는 다음 실행(push, 본문 수정, 다시 열기) 때에야 잡히므로, 검사가 지키는 것은 본문이다. 사이드바에서 이슈를 이었을 수 있으면 머지 전에 아래 명령으로 다시 확인한다. PR을 API로 읽으므로 위의 로컬 검사에는 없다. 열린 PR은 `python3 scripts/check_pr_refs.py codeyoma/krx-quant <PR 번호>`로 확인한다.

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
- 순위표(`/v1/leaderboard`)와 알고리즘 상세 통계(`/v1/algorithms/<이름>/stats`)를 낸다. 방향 정밀도, 커버리지, 기준 예측 대비 lift, 닿음 손익(정답과 같은 규칙, 연구용. 하락을 주식 공매도로 친 것과 인버스 상품으로 친 것, #267), Brier 점수, 보정 곡선을 신뢰구간과 함께 보여준다. 상세 통계는 기준율 대비 실력(kappa, Gerrity, 균형 맞힘률, 독립성 검정)과 변동성 3분위별 성적도 낸다(#262).
- 같은 봉 마감에 방향 말고 변동폭(4분 동안 통합 중간가의 최고 − 최저, #180)도 예측한다: 기준 예측 2종(시간대 기후, 직전 4분 지속)과 첫 모델 Mandelbrot(#181, HAR 방식 로그 범위 회귀). Mandelbrot은 사전 검증(`prevalidate mandelbrot`)을 통과해야 돈다. 순위표는 `/v1/leaderboard?kind=range`다([docs/engine-api.md](docs/engine-api.md#변동폭-177-180)).
- 장벽(#183)도 예측한다: 봉 마감 중간가 ± 폭 w(1분 변화의 지수가중 표준편차 × √4, 비용 k보다 좁지 않게)에 둔 위·아래 장벽 중 4분 안에 어느 쪽에 먼저 닿을지(위 먼저·시간 만료·아래 먼저, 삼중 장벽). 기준 예측 2종(시간대 기후, 직전 4분 지속)과 첫 모델 Breiman(#184, LightGBM 3분류)이 예측하고, 연구용 장벽 손익을 함께 낸다. Breiman은 사전 검증(`prevalidate breiman`)을 통과해야 돈다. 순위표는 `/v1/leaderboard?kind=barrier`다([docs/engine-api.md](docs/engine-api.md#장벽-178-183)). 같은 장벽을 시간 장벽 15·30·60분으로 둔 `barrier_15m`, `barrier_30m`, `barrier_60m`(#263, 폭은 σ̂₁ × √H)도 기준 예측 2종씩과 지평마다의 Breiman(`breiman-15m`, `breiman-30m`, `breiman-60m`, 사전 검증 뒤)으로 예측한다.
- 하루에 한 번(거래일 08:50) 그날 KRX 시가 단일가 → 종가 단일가의 방향(상승·보합·하락, 보합 폭은 종목 변동성으로 세 클래스가 1/3씩 되게)도 예측한다(`open_to_close`, #264): 기준 예측 3종(늘 보합, 기후, 어제 방향)과 첫 모델 Cox(다항 로지스틱 회귀, 밤사이 미국 반도체·전일 수급·NXT 프리마켓 등, KRX 일봉 10년 walk-forward 사전 검증 뒤). 정답은 그날 15:30 종가 단일가 가격(KIS KRX 1분봉, 15:40에 받는다)이 저장된 뒤(15:45부터) 쓴다(정답 버전 2, #317. 그 저녁 저장한 KRX 일봉의 종가는 KIS의 잠정 값, 곧 애프터마켓의 마지막 가격이고, KIS가 확정한 종가는 종가 단일가다. 수집은 마지막 5개 봉을 다시 받는다, #264). 순위표는 `/v1/leaderboard?kind=open_to_close`다([docs/engine-api.md](docs/engine-api.md#하루-한-번-예측-종류-264)).

  > **바뀜** (2026-10-15, #209): "지금은 기준 예측 2종(시간대 기후, 직전 4분 지속)이 예측하고" → 첫 모델 Breiman도 예측한다(사전 검증 뒤 수습). 이유: #184가 넣었다.
- 알고리즘은 녹화된 날은 녹화 이벤트(호가로 만든 도달 정답)로, 백필만 있는 날은 1분봉(1분봉 규칙의 도달 정답)으로 학습한다. 모델은 `state/models/`에 저장되고, 실행 중인 엔진은 30초마다 확인해 바뀐 모델로 바꾼다.
- `KRX_TARGET_VERSION`(#169)은 기본 3(도달 정답, #170. 뒤집힌 호가도 센다, #187)이다. `1`이면 학습 정답, 알고리즘의 공통 정규분포 함수, 기준 예측(3종), 사전 검증이 옛 정답(4분 뒤 중간가 변화)을 따르지만, 채점은 그대로 도달 정답이라 수습이 세어지지 않는다([docs/engine-api.md](docs/engine-api.md#학습)). `2`(뒤집힌 호가를 버린 도달 정답)는 설정 오류다. 버전마다의 뜻은 [docs/engine-api.md](docs/engine-api.md#채점)의 "정답 버전"에 있다.

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
- 서버는 매일 밤 22:30부터, 밤 학습이 끝난 뒤 때가 된 예약 작업(진단, 녹화 10거래일 뒤의 사전 검증 등)을 하나씩 돌리고 결과를 GitHub 이슈 댓글로 남긴다([docs/operations.md](docs/operations.md#예약-작업-215), #215).
- 서버는 매일 06:00(KST)에 main의 새 커밋을 받아 스스로 다시 띄운다. 새 버전이 뜨지 않으면 이전 커밋으로 되돌리고 텔레그램으로 알린다(`deploy/auto-update.sh`).
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

서비스는 둘이다: 수집기·엔진(`engine`)과 자동 매매 trader(`trader`, 모의뿐, #394). 같은 이미지의 다른 컨테이너라 하나가 죽어도 다른 하나는 돈다. 엔진 API의 포트만 Tailscale 주소에 열리고, 수집기가 trader에게 호가를 보내는 포트(8081)는 compose 네트워크 안에만 있다.

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

서버 없이 엔진 API를 띄운다(#20). 지어낸 아침(2026-10-12 09:00–10:30, 3종목, KOSPI·선물·달러선물, 서킷브레이커 1단계와 VI 한 번)을 실제 엔진으로 재생하고, 실제 API 코드로 답한다. 코스피가 09:04에 8.5% 떨어져 09:06–09:36에 서킷브레이커 1단계가 걸린다. 그 30분은 세 종목 모두 체결과 호가가 없어 1분봉이 비고, 000660은 09:40:10–09:42:20에 VI(동적)가 걸린다. 둘 다 `GET /v1/symbols/{종목}/special`과 `status.special`에 나온다(#236). 선물은 떨어지지 않아 사이드카는 없다. 늘 같은 응답이다. 다만 `status.cpu`의 시간은 실제로 재서 실행마다 조금 다르다. 시계는 10:30에 멈춰 있고, `status.mode`는 `demo`다. 모든 알고리즘을 정식(`active`)으로 두어 합의도 나온다. 그날 07:30 매크로 읽기는 위험회피(`risk_off`)로 정해져 있다. KIS 상태(`status.kis`, 결측 피드 하나)와 Newton의 모델 이력도 가상 값으로 보인다. 그 앞 두 거래일(2026-10-07, 10-08. 10-09는 한글날 휴장)의 14:00–15:20도 미리 재생해 둔다(#108). 그날들의 예측과 채점은 DB에, 1분봉은 백필과 같은 파일로 있어 차트를 지난 날로 스크롤해 볼 수 있다. 종목마다 10-08까지 지어낸 일봉 500개와 그것을 묶은 주·월·년봉도 있어 차트의 "일·주·월·년" 보기를 거슬러 볼 수 있다(#111, #112). 같은 500일에는 지어낸 일별 수급 8종(공매도, 개인·기관·외국인 순매수, 외국인 보유수량·지분율, 대차·신용잔고)도 있어 `GET /v1/symbols/{종목}/supply`로 하단 지표를 그려 볼 수 있다(#226. 10-12 아침 그날의 값은 아직 없다). KRX 마감 흐름(#305)도 있다: 16종의 지어낸 순자산(10-08의 0198D0 하나는 없다)으로 10:30의 엔진 기준가에서 계산한 그날 수요와, 일봉에서 지어낸 10-08까지 20거래일의 표(마지막 8일은 저장한 15:20 수요)가 `GET /v1/symbols/{종목}/closing-flow`, `/closing-flow/days`로 나온다. 상품 이름도 지어낸 것이다. Fincept 화면의 엔진 주소를 `http://127.0.0.1:8090`으로 바꿔 붙인다. DB는 `state/demo.sqlite3`(`--db`)에 새로 만들고, 매크로 기록, 모델 이력, 지난 날의 1분봉, 일봉, 일별 수급은 그 옆 `state/demo-data/`(`macro/`, `state/models/`, `bars/`, `candles/`, `state/supply/`)에 새로 쓴다. 응답은 실제 엔진과 같은 스키마를 지킨다.

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
| `trade` | 자동 매매(#394): 엔진 API의 비용 지도(`KRX_TRADER_ENGINE_URL`)가 시장가 왕복으로 통과시킨 칸들(#395, 시작할 때와 매일 08:30에 고른다)의 예측을 엔진 DB에서 읽기만 하며 005930을 삼중 장벽으로 모의 매매하고 `state/trader.sqlite3`에 남긴다. 호가는 수집기의 호가 스트림(`KRX_TRADER_QUOTES_URL`)에서 받는다. 서버에서는 compose 서비스 `trader`가 돌린다(수집기와 따로). 멈출 때까지 돈다([docs/operations.md](docs/operations.md#자동-매매-394)) | 없어도 된다 |
| `doctor [--api 주소]` | 서버 요약을 출력한다(#322): 엔진의 `commit`·`started_at`, KIS 연결(`connected`, `last_frame_at`, `seconds_since_frame`)과 피드·키별 `last_received_at`·`gap`, 오늘의 PINGPONG(시간별 수, 마지막, 가장 긴 간격), 예약 작업(마지막 실행, 끝까지 돈 적 없는 작업, 올리지 못한 결과), 디스크와 메모리, 밤 학습(#355), Loki 전송(켜짐·꺼짐, 보낸·버린 줄, 마지막 성공과 오류의 종류, #358). 엔진 상태는 엔진 API(`/v1/status`)에 묻는다: 이 컨테이너의 것, 그다음 compose의 `engine` 서비스(`docker compose run`). 닿지 않으면 DB의 마지막 시작을 쓴다. 엔진이 메모리에 둔 최근 로그 줄은 다른 프로세스라 읽지 못한다고 적고, 엔진의 전체 줄이 Loki에 있다고 적는다. **시세 값은 출력하지 않고**, 예약 작업의 댓글처럼 가린다. 읽기만 한다. 서버에서는 `docker compose exec engine krx-quant doctor`. 에이전트는 이슈에 `needs-server-snapshot` 라벨을 붙여 같은 요약(엔진의 최근 로그 줄 포함)을 댓글로 받는다([docs/operations.md](docs/operations.md#서버-상태-보기-322)) | 없어도 된다 |
| `measure-subscriptions` | 세션당 실시간 구독 한도를 잰다. 수집기를 멈추고 실행한다 | 필요 |
| `probe-candles` | KIS 기간별 시세(`FHKST03010100`, 일·주·월·년봉)가 주는 것을 재서 출력한다(#111): 종목·ETF의 시장 구분(`UN`·`J`·`NX`)별 응답과 필드, 기간마다 한 번에 주는 개수와 거슬러 가는 끝, 수정주가 인자(`FID_ORG_ADJ_PRC`)의 차이. 읽기만 하고 0.5초 간격으로 부르므로 수집기가 도는 중에 실행해도 된다(2–3분, 300번 안팎). 결과는 [docs/kis-feeds.md](docs/kis-feeds.md#기간별-시세-일주월년봉)에 옮긴다 | 필요 |
| `probe-supply` | 일별 수급 지표(#226)를 줄 KIS 조회 7개(공매도, 투자자별 순매수, 외국인 보유·소진율, 대차·신용잔고)와, `TOSS_CLIENT_ID`·`TOSS_CLIENT_SECRET`이 있으면 토스증권 Open API 조회 5개(#235)를 불러 출력한다: 종목·ETF의 시장 구분별 응답, 한 번에 주는 행 수와 날짜, 필드마다 비었는지·0인지·정수(수량)인지·소수(비율)인지, 첫 종목을 거슬러 간 끝(토스는 기록의 부분마다 어느 날부터 null이 아닌지도), 같은 값을 주는 조회끼리(토스와 KIS도) 같은 날 수. **시세 값은 출력하지 않는다**(공개 이슈에 올라간다). 읽기만 하고 KIS는 0.5초, 토스는 0.2초 간격으로, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다(몇 분). 서버에서는 예약 작업 `probe-supply-226`이 배포 뒤 첫 밤에 돌려 #226에 남긴다([docs/kis-feeds.md](docs/kis-feeds.md#일별-수급-226), [docs/toss-feeds.md](docs/toss-feeds.md), [docs/operations.md](docs/operations.md#일별-수급-출처-확인-226)). 하나도 답을 못 받으면 종료 코드 1 | 필요 |
| `probe-net-buys` | KIS 투자자 순매수(`FHPTJ04160001`)를 KRX(`J`)와 통합(`UN`)으로 2016, 2020, 2024-06, 2025-02, 2025-03, 오늘까지의 30거래일씩 묻고(#264, #297), 답마다 행 수, 필드 모양, 투자자마다 값(0이 아닌 순매수)이 있는 날 수와 첫날·마지막 날, NXT 개장(2025-03-04) 뒤 `J`와 `UN`이 같은 날 수를 출력한다. 그리고 저장된 KIS 수급의 개장 전·뒤 날을 시장(`net_market`)과 값 있는 날로 세고, 개장 전 KIS `J`와 토스가 같은 날 수를 낸다. **시세 값은 출력하지 않는다.** 세 종목이면 36번, 0.5초 간격, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-net-buys-j-264`가 배포 뒤 첫 밤에 돌려 #264에 남긴다([docs/operations.md](docs/operations.md#krx-순매수-확인-264)). 쓸 수 있는 답(행 목록)을 하나도 못 받으면 종료 코드 1 | 필요 |
| `probe-close` | 최근 1년의 KRX(`J`) 일봉 종가가 무엇인지 센다(#264, #317): 종목마다 날마다 KIS KRX 1분봉(`FHKST03010230`, 하루 2–3번)을 묻고, 애프터마켓(2026-09-14) 전과 뒤로 나눠 일봉 종가가 종가 단일가 봉(장 마감부터 10분 안의 첫 봉)의 종가와 같은지, 그날 마지막 봉의 종가와 같은지, 일봉 시가가 시가 단일가 봉의 시가와 같은지, 녹화가 있는 날은 녹화한 종가 단일가가 같은지, 그리고 봉 시각을 출력한다. 애프터마켓 뒤 종가가 단일가 봉과 다른 날의 날짜, 최근 10일의 KIS가 지금 주는 KRX 일봉(종목마다 1번 더)과 저장한 봉의 다른 필드, 수집기가 KIS가 바꿔 바꾼 봉의 기록(날짜, 실행 시각, 필드)도 낸다. **가격은 출력하지 않는다.** 세 종목이면 약 1,600번, 0.25초 간격, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-close-264`가 배포 뒤 첫 밤에, `probe-close-refresh-264`가 일봉 다시 받기(#264) 배포 뒤 첫 거래일 밤에 돌려 #264에 남긴다([docs/operations.md](docs/operations.md#하루-한-번-예측-264)). 쓸 수 있는 답을 하나도 못 받으면 종료 코드 1 | 필요 |
| `probe-dividends` | KIS 수정주가 일봉이 현금배당을 빼는지 잰다(#310): 종목마다 최근 10년의 KRX(`J`) 일봉(`FHKST03010100`)을 수정주가와 원주가로 받아 두 종가의 비가 바뀌는 날(계단)을 크기 구간·달별로 세어 그 날짜와 함께, KIS 일봉 행의 락 구분(`flng_cls_code`) 등 표시의 값별 날 수와 그날 계단이 있는지를 출력한다. **가격은 출력하지 않는다.** 세 종목이면 150번쯤, 0.5초 간격, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-dividends-310`이 배포 뒤 첫 밤에 돌려 #310에 남긴다([docs/operations.md](docs/operations.md#배당과-수정주가-확인-310)). 쓸 수 있는 답을 하나도 못 받으면 종료 코드 1 | 필요 |
| `probe-nav` | 단일종목 ±2배 ETF 16종(`products.PRODUCTS`)의 순자산을 KIS 어디서 받을 수 있는지 잰다(#305): 상품마다 ETF/ETN 현재가(`FHPST02400000`)의 순자산 총액, NAV 비교추이 일별(`FHPST02440200`, 상장 전부터 거슬러)·종목(`FHPST02440000`)·분별(`FHPST02440100`)을 묻고, 답과 필드 모양, 값이 있는 상품 수, 배율·상장일이 상품 목록과 같은지, 일별이 상장일(2026-05-27)까지 가는지와 빠진 거래일, 그날 값이 물은 시각에 들어와 있는지, 순자산 총액이 상장 주수 × NAV와 어느 단위에서 같은지를 출력한다. **시세 값은 출력하지 않는다.** 100번 남짓, 0.5초 간격, 캐시된 토큰으로 부르므로 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-nav-305`가 배포 뒤 첫 거래일 밤에 돌려 #305에 남긴다([docs/operations.md](docs/operations.md#etf-순자산-출처-확인-305)). 쓸 수 있는 답을 하나도 못 받으면 종료 코드 1 | 필요 |
| `probe-krx-etf` | KRX Open API의 ETF 일별매매정보(`etf_bydd_trd`)가 단일종목 ±2배 ETF 16종에 무엇을 주는지 잰다(#305): 상장 전 거래일, 상장일(2026-05-27), 그 뒤 주마다 첫 거래일, 마지막 5거래일을 묻고, 날마다의 답(401·403·429는 KRX FAQ의 뜻과 함께), 필드와 모양(서비스 페이지의 19개와 견줘), 종목코드가 든 필드, 상품마다 상장일부터 행이 있는지, 물은 때 마지막 거래일과 그 전 거래일이 들어와 있는지, 상장좌수(`LIST_SHRS`)가 날마다 바뀌는지와 시가총액 ÷ 종가와 같은지, 순자산총액이 상장좌수 × NAV와 어느 단위에서 같은지, 같은 날의 KIS NAV(`FHPST02440200`)와 순자산 총액·주수(`FHPST02400000`)와 같은지를 출력한다. **시세 값은 출력하지 않는다**(KRX 약관: 제3자 제공 금지). `KRX_OPENAPI_KEY`가 있어야 하고 헤더로만 보낸다. KRX 30번쯤 1초 간격, KIS 60번쯤 0.5초 간격(캐시된 토큰)이라 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-krx-etf-305`가 키를 넣은 뒤 첫 밤에 돌려 #305에 남긴다([docs/krx-openapi.md](docs/krx-openapi.md), [docs/operations.md](docs/operations.md#krx-open-api-etf-일별매매정보-확인-305)). 행이 있는 답을 하루도 받지 못하면 종료 코드 1 | 없어도 된다(8절만 건너뛴다) |
| `check-fallbacks` | 한도로 잘린 구독을 대신하는 REST 조회를 하나씩 한 번 부르고, 기록할 이벤트나 실패 이유를 출력한다. 기록하지 않고 REST만 써서 수집기가 도는 중에 실행해도 된다. 실패가 있으면 종료 코드 1([docs/kis-feeds.md](docs/kis-feeds.md#rest-대체-조회)) | 필요 |
| `backfill` | 빠진 1분봉을 한 번 채운다. 수집기를 멈추고 실행한다 | 필요 |
| `archive [--recheck]` | 녹화한 날을 합쳐 `KRX_OBJECT_STORE`에 올린다. `--recheck`는 모든 파일을 저장소와 비교한다 | 필요(쓰지는 않는다) |
| `replay <YYYY-MM-DD> [--db 경로]` | 녹화한 하루를 엔진으로 다시 돌려 별도 DB에 쓴다. 요약은 도달 정답으로 센 채점 수다(#170, 전에는 기준 A) | 없어도 된다 |
| `rescore` | 2026-10-12부터의 지난 예측의 도달 정답(`touches`)을 녹화로 다시 계산해, 다른 행만 바꾼다. 옛 채점(`class_a`·`class_b`)과 특수 표시는 그대로다. 엔진이 돌고 있으면 거부한다(종료 코드 1). 엔진을 멈추고 장 시간 밖에 돌린다. 빠진 정답은 엔진이 시작할 때 저절로 채운다([docs/operations.md](docs/operations.md#지난-예측의-도달-정답-168), #168) | 없어도 된다 |
| `fit <알고리즘> [--days N]` | 최근 날들로 학습해 `state/models/`에 설치한다. 녹화된 날은 녹화 이벤트(중간가), 백필만 있는 날은 1분봉(종가)으로. 다른 종류의 알고리즘(변동폭의 `mandelbrot`, #181, 장벽의 `breiman`, #184, 15·30·60분 장벽의 `breiman-15m` 등, #263)은 그 종류의 정답으로 | 없어도 된다 |
| `prevalidate <알고리즘> [--days N]` | 새 알고리즘을 녹화된 최근 20거래일(최소 10일)로 재생해 사전 검증한다. 통과하면 엔진이 다음 봉 마감부터 수습으로 돌린다. 거부되면 종료 코드 1([docs/engine-api.md](docs/engine-api.md#사전-검증-24)). 변동폭의 `mandelbrot`(#181)과 장벽의 `breiman`(#184)도 이것으로 검증한다. 하루 한 번 예측 종류의 `cox`(#264)는 녹화 재생 대신 KRX 일봉 10년의 walk-forward로 검증한다(`--days`는 쓰지 않는다. 서버의 예약 작업은 녹화 일수를 기다리지 않고 따로 돈다). 서버에서는 예약 작업이 녹화 10거래일부터 밤마다 하나씩 돌려 결과를 #204에 남긴다(#204). 실행마다 `state/prevalidations/<이름>.history.jsonl`에 한 줄을 남기고, 전에 돈 적이 있으면 그 횟수와 같은 날을 재생한 횟수를 출력한다(#262). 같은 날로 다시 돌리면 통과 기준이 더 엄격하다(거래일 단위 하한, Bonferroni) | 없어도 된다 |
| `train [--day YYYY-MM-DD] [--from-store]` | 밤 학습 한 번. `--day`가 없으면 Object Store에 모든 종목의 백필 1분봉이나 녹화된 체결이 있는 가장 최근 날. `--from-store`는 서버가 S3에 올린 설정과 휴장일 달력으로 학습한다(EC2 Spot, #23) | 없어도 된다 |
| `registry-password` | ECR 레지스트리의 `docker login` 암호를 출력한다. `deploy/push-image.sh`가 쓴다(#23) | 없어도 된다 |
| `macro` | 오늘의 매크로 지표를 한 번 읽고 국면을 정해 `data/macro`에 기록한다(#8). 실패한 출처가 있으면 종료 코드 1 | 없어도 된다 |
| `diagnose [--days N]` | 판단에 필요한 실측(표시 지연과 채점 수, 특수 상황 비율과 예측 상태, 봉 마감 스프레드, 피드 수신 지연, 매크로 결측, 도달 정답의 분포·폭·표시 전 닿음, 변동폭 정답의 분포와 1분봉 규칙 일치율(#180), 장벽 정답의 분포·폭·1분봉 규칙 일치율·미래를 알았을 때의 손익(#183), 예측자별 기준율 대비 실력과 봉 마감 +1·+5·+30초 진입의 목표 도달·닿음 손익(#262))을 텍스트로 출력한다(#170부터 기준 A·B 차이는 없다). 읽기만 한다. `--days`는 데이터가 있는 최근 N거래일, 기본 20([docs/operations.md](docs/operations.md#진단-82)) | 없어도 된다 |
| `signal-review [--days N]` | 텔레그램 신호 기준(`KRX_SIGNAL_MIN_PROBABILITY`, `KRX_SIGNAL_MIN_AGREE`)을 정할 실측을 텍스트로 출력한다(#206): 지금 설정과 엔진이 남긴 신호, 순위표(상승·보합·하락 예측별 적중률), 정식 알고리즘의 정식 뒤 성적, 기준 후보(최종 합의 확률 × 같은 방향 정식 수)마다 엔진의 신호 규칙으로 낸 신호 수와 상승·하락 적중률(95% 구간). 읽기만 한다. `--days`는 예측이 있는 최근 N일, 기본 20. 서버에서는 예약 작업이 정식 알고리즘이 처음 생긴 밤에 한 번 돌려 #206에 남긴다([docs/operations.md](docs/operations.md#신호-기준-검토-206)) | 없어도 된다 |
| `probe-night [--day YYYY-MM-DD]` | 녹화한 KOSPI200 야간선물의 한 밤(그날 18:00 – 다음 날 06:00, #264)을 요약해 출력한다: KIS의 구독 응답, 체결(`H0MFCNT0`)·호가(`H0MFASP0`)의 프레임·레코드 수, 레코드당 필드 수, 첫·마지막 수신과 시간대별 수, 시각 필드의 원문, events 행과 정규화하지 못한 레코드, 결측 구간, KIS 마스터의 KOSPI200 선물 코드와 야간선물 마스터 앞 10줄. **시세 값은 출력하지 않는다**. 녹화 파일과 공개 마스터 파일만 읽어 수집기가 도는 중에 실행해도 된다. 기본은 18:00 전부터 구독한 가장 최근의 끝난 밤이다. 서버에서는 예약 작업 `probe-night-264`가 첫 밤 뒤에 돌려 #264에 남긴다([docs/operations.md](docs/operations.md#야간선물-녹화-확인-264)) | 없어도 된다 |
| `probe-expected-open [--days N]` | 녹화한 끝난 거래일(기본 가장 최근 10일)의 raw 통합 호가(`H0UNASP0`)에서 단일가의 예상 체결(#304)을 센다: 시간대(장전, 시가 단일가, 연속매매, 종가 단일가, 장 마감 뒤)별 레코드와 예상가가 있는 레코드, 거래소 구분·시간 구분 코드, KRX(거래소 구분 `1`) 예상가를 처음·마지막으로 본 시각(다른 코드는 괄호로 따로, #341), 단일가 끝 직전 KRX 예상가와 KRX 일봉 시가·종가의 차이(틱), REST 호가 스냅샷의 예상 체결 필드, events의 새 열. **시세 값은 출력하지 않는다**. 녹화 파일만 읽어 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-expected-open-314`가 배포 뒤 첫 거래일 밤에 돌려 #314에 남겼고, KRX 것만 세는 `probe-expected-open-krx-314`가 #341 배포 뒤 첫 거래일 밤(2026-10-21)에 다시 돌려 남겼다([docs/operations.md](docs/operations.md#예상-체결-녹화-확인-304)) | 없어도 된다 |
| `probe-gaps` | 녹화한 모든 날의 WebSocket 결측과 종가 단일가 구간을 센다(#344): 날마다 `gaps`의 구간을 이유·시작 시각·길이·피드·키로(한 끊김이 구독마다 여는 구간은 한 줄로 묶어 피드·키 수와 길이의 범위로), 거래일마다 마감 10분 전부터 마감 1분 뒤까지(보통 15:20–15:31) WebSocket 피드별 마지막 프레임 시각과 수, 모든 프레임 사이의 가장 긴 침묵, 겹친 결측, KRX(`H0STCNT0`)·통합(`H0UNCNT0`) 체결의 종목마다 마감 뒤 1분 안의 첫 체결(시각과 원문 코드, 종가 단일가 체결의 표시가 문서에 없어 이것으로 본다), `bars`의 1분봉 채움. **시세 값은 출력하지 않는다**. 녹화 파일만 읽어 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-gaps-344`가 배포 뒤 첫 밤에 돌려 #344에 남긴다([docs/operations.md](docs/operations.md#websocket-결측-확인-344)) | 없어도 된다 |
| `probe-member-frames` | 녹화한 모든 날의 회원사(`H0UNMBC0`) raw 프레임 중 필드 수가 레코드 수 × 78이 아닌 것을 센다(#381): 날마다·시(KST)마다 그 수, 그런 프레임마다 종목·받은 시각·필드 수와 칸마다의 모양(빈 값, 숫자, 부호나 소수점이 있는 수, Y·N, 글자)을 같은 종목의 다음 78칸 프레임의 모양과 나란히, 앞·뒤에서 모양이 같은 칸 수와 빠진 칸이 한 덩어리라면 시작할 수 있는 칸, 그 분의 1분 앞부터 1분 뒤까지 다른 WebSocket 피드의 필드 수가 맞지 않는 프레임과 `_unparsed` 프레임. **필드의 값은 출력하지 않는다**. 녹화 파일만 읽어 수집기가 도는 중에 실행해도 된다. 서버에서는 예약 작업 `probe-member-frames-381`이 배포 뒤 첫 밤에 돌려 #381에 남긴다([docs/operations.md](docs/operations.md#회원사-프레임의-필드-수-확인-381)) | 없어도 된다 |
| `check-closing-flow` | KRX 마감 흐름(#305)이 서버에 쌓은 것을 센다: ETF 순자산의 출처별 행 수와 KIS 행의 날마다 상품 수·처음 받은 때(KIS가 다음 날로 넘기는 때), 15:20 수요 행, 지난날 표의 수요·체결대금 출처, 그리고 녹화한 종가 단일가가 있는 날마다 통합 1분봉(백필 파일)과 KRX(`J`) 1분봉(KIS에 그날 한 번, 0.5초 간격)의 단일가 봉을 녹화한 체결대금과 견준다. 쓰는 것은 없고 **시세 값은 출력하지 않는다**(개수, 날짜, 시각, 비율만). 서버에서는 예약 작업 `check-closing-flow-305`가 배포 뒤 첫 거래일 밤에 돌려 #305에 남긴다([docs/operations.md](docs/operations.md#krx-마감-흐름-확인-305)) | 필요 |
| `check-krx-net-assets` | KRX Open API에서 받은 ETF 순자산(#305, `state/closing_flow/net_assets.parquet`의 `krx` 행)을 센다: 상장일부터 거래일마다 행이 있는지와 빠진 상품, 마지막 10일의 처음 받은 때, 순자산총액이 어느 날의 상장좌수 × NAV와 같은지(그날, 다음 거래일, 전 거래일의 주수, 전 거래일의 NAV), 같은 날 KIS의 순자산과 같은지. 파일만 읽고 아무것도 묻지 않으며 **값은 출력하지 않는다**(개수, 날짜, 시각, 상품 코드만. KRX 약관). 서버에서는 예약 작업 `check-krx-net-assets-305`가 키가 있는 첫 거래일 밤에 돌려 #305에 남긴다([docs/operations.md](docs/operations.md#krx-순자산-확인-305)) | 없어도 된다 |
| `structure-check [--days N]` | 리서치(`research/reports/`)의 시장 구조 주장을 저장된 1분봉, 일봉, KIS 일별 수급으로 재 텍스트로 출력한다(#270): 2026-05-27 단일종목 레버리지 ETF 상장 전후·07-31·09-14로 나눈 일 단위 구조(ρ1, 다음날 되돌림, 갭 방향), 마감 단일가의 움직임과 다음날 반전, 4분 도달 정답(1분봉 규칙)의 클래스 비율과 직전 창 적중률(기간·틱 크기별), 월말·월초 × 전날 외국인 순매수. 가격·거래량·수급 값은 출력하지 않는다. 읽기만 하고 API를 부르지 않는다. `--days`는 1분봉이 있는 최근 N거래일, 기본 전부. 서버에서는 예약 작업이 배포 뒤 첫 밤에 한 번 돌려 #270에 남긴다. #295 뒤 한 번 더 돌려 머리말과 4절을 #264에 남긴다([docs/operations.md](docs/operations.md#구조-실측-270)) | 없어도 된다 |
| `daily-check [--days N]` | 최근 N거래일(기본 5)의 하루 한 번 예측 종류(#264, `open_to_close`)를 텍스트로 출력한다: 08:50 예측(예측자·상태별 수, 보합 폭), 정답(클래스별 수, 출처, 특수, 채점 시각), KRX 일봉의 시가·종가와 받아 둔 종가 단일가(정답의 종가, #317)가 녹화한 KRX 체결의 단일가와 같은지. 가격은 출력하지 않는다. 읽기만 한다. 서버에서는 예약 작업이 배포 뒤 첫 거래일 밤에 한 번 돌려 #264에 남긴다([docs/operations.md](docs/operations.md#하루-한-번-예측-264)) | 없어도 된다 |
| `simulate daily`, `simulate bar-close` | 수습 시뮬레이션(#298). `daily`: 하루 한 번 예측 종류(`cox`)의 수습이 시뮬레이션을 기다리면 최근 20거래일을 그 알고리즘이 수습으로 돌았다고 치고 재생하고(날마다 그 전날까지로 다시 학습), 20일째 판정을 바로 한다. 통과하면 정식, 못 하면 수습이 이어진다(다음 판정 40일째). 사전 검증을 통과했지만 엔진이 아직 올리지 않은 알고리즘은 수습에 올린다. 그 종류의 최근 정답이 다 들어오지 않았거나(`open_to_close`: 애프터마켓부터의 종가 단일가 가격) 사전 검증이 지금 코드·정답 버전·입력의 것이 아닌 수습은 시뮬레이션하지 않고 기다린다(`<이름>: its simulation waits - …`, #264, #317. #315의 판정 보류는 #317에서 없어졌다). 시뮬레이션 예측은 실시간 예측과 따로 남고 순위표·통계에는 들지 않는다. 엔진이 도는 중에도 된다. `bar-close`: 4분 종류(방향, 변동폭, 장벽)의 수습이 시뮬레이션을 기다리면 녹화된 최근 거래일(10–20일, 10일보다 적으면 하지 않는다)을 첫날 전까지로 한 번 학습해 재생하고 바로 판정한다(몇 시간 걸릴 수 있다). 서버에서는 예약 작업 `simulate-daily-298`(사전 검증을 통과한 그 밤, #298과 #264)과 `simulate-bar-close-298`(녹화 10거래일부터, #298과 #204)이 돌린다([docs/engine-api.md](docs/engine-api.md#상태-관리-17)) | 없어도 된다 |
| `rollback <알고리즘>` | 직전 모델로 되돌린다. 도달 정답에서는 그 정답으로 학습한 모델로만(#193) | 없어도 된다 |
| `retire <알고리즘>`, `unretire <알고리즘>` | 은퇴시킨다, 은퇴를 풀어 수습으로 되돌린다 | 없어도 된다 |
| `test-alert` | 텔레그램에 `[테스트]` 메시지를 보낸다 | 필요(쓰지는 않는다) |
| `notify <글>` | 텔레그램에 `[알림] <글>`을 보낸다. `deploy/auto-update.sh`가 쓴다 | 없어도 된다 |
| `fake-kis [--port 18080] [--host 127.0.0.1] [--trades]` | 가짜 KIS 서버(아래 "키 없이 확인하기") | 없어도 된다 |
| `demo-engine [--port 8090] [--host 127.0.0.1] [--db state/demo.sqlite3] [--pace 초]` | 데모 엔진 API(아래 "화면을 만들 때"). `--pace`는 10:00부터 이어 재생하며 스트림을 보낸다 | 없어도 된다 |

> **삭제됨** (2026-10-23, #360): `logs-bundle [--days N | --since 때] [--grep 패턴]`(상태 폴더의 로그 파일 `state/logs/`를 가려서 한 파일로 묶던 명령, #356). 이유: 사용자가 로그를 Grafana Cloud Loki로만 남기기로 정했다(2026-10-23). 대신: Loki에서 질의한다([docs/agents/server-logs.md](docs/agents/server-logs.md)).

종료 코드: 0은 성공이다. 2는 설정 오류(`configuration error: ...`)나 잘못된 인자다. 1은 KIS 오류(`KIS error: ...`, Docker가 다시 띄운다)이거나 명령이 할 일을 못 한 경우다. 예: 업로드에 실패한 파티션, 학습할 1분봉 없음, 되돌릴 모델 없음, 텔레그램 전송 실패, 사전 검증 거부, 실패한 REST 대체 조회, 답을 하나도 받지 못한 `probe-supply`.

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
| P4 | 밤 학습 | 코드 완료. 밤 학습은 상시 서버에서 돈다. Spot은 계정의 서비스 제어 정책이 막아 보류했다(2026-10-14, #23, docs/design.md 8장): 상시 서버의 밤 학습과 08:00 교체(PR #45), 녹화 이벤트로 학습하기(#22, PR #48), EC2 Spot 학습과 비용 상한(PR #67, `deploy/training.yaml`). Spot 절차(docs/operations.md 3장)는 고급 기능을 활성화해 그 정책을 바꾼 계정에서만 쓸 수 있다 |
| P5 | 호가 알고리즘, 재생 테스트 | 구현 완료: 하루 재생(`replay`), 새 알고리즘 사전 검증(`prevalidate`, #24), Kalman(#25), Shannon(#26), Poisson(#27). 셋은 녹화 10거래일 뒤 사전 검증을 통과해야 실시간에서 돈다 |

## 비밀값

API 키와 토큰은 서버의 `.env`에만 둔다. 저장소에 커밋하지 않는다.
