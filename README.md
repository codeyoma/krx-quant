# krx-quant

KRX 종목의 4분 뒤 가격 움직임을 여러 퀀트 알고리즘이 예측하고, 예측을 자동 채점해 통계로 보여주는 개인용 엔진이다. 결과는 fork한 Fincept Terminal([codeyoma/FinceptTerminal](https://github.com/codeyoma/FinceptTerminal), `custom` 브랜치)의 화면에서 본다.

주문 기능은 없다. 신호만 낸다.

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
uv run python scripts/check_forbidden.py && uv run ruff check && uv run mypy && uv run pytest -q
```

`scripts/check_forbidden.py`는 KIS 주문 TR 코드, 주문 엔드포인트, 비밀값 패턴이 저장소에 들어오면 실패한다. CI도 같은 검사를 한다.

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
- **결측 기록**: 끊긴 구간과 연속매매 시간에 조용해진 피드는 `gaps` 계층에 기록한다. 거래일에는 개장 후 한 번도 데이터가 오지 않은 구독도 지연으로 본다. 휴장일에는 판정하지 않는다.
- **토큰 재사용**: REST 접근 토큰은 `state/kis_token.json`(권한 600)에 캐시해서 재시작해도 다시 발급하지 않는다. 만료 1시간 전부터 갱신하고, 발급 간격은 61초 이상으로 둔다(KIS는 분당 1회).
- **텔레그램 알림**: `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`를 넣으면 장애가 시작될 때 한 번, 복구될 때 한 번 텔레그램으로 알린다. 넣지 않으면 로그만 남긴다.
  - 재연결 복구 알림은 연결이 60초(`KRX_RECOVERY_HOLD_SECONDS`) 이상 유지된 뒤에 보낸다. 그래서 끊겼다 붙기를 반복해도 알림이 매번 가지 않는다.
  - 지연 알림은 새로 지연된 피드들을 한 메시지로 묶어 보내고, 다시 들어온 피드도 묶어서 알린다.
  - 예측 신호(`[신호] ...`)는 선택이다. `KRX_SIGNAL_ALERT=on`이면 최종 합의가 충분히 확실하고 정식 알고리즘 N개 이상이 같은 방향일 때 보낸다([docs/engine-api.md](docs/engine-api.md#텔레그램-신호-알림-19)).

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
- 수집기는 받은 이벤트로 1분봉 마감마다 기준 예측 3종(항상 보합, 직전 4분 방향 지속, 클래스 비율 무작위), 알고리즘(Newton 추세 추종, Gauss 역추세, Granger 이웃 관찰, Markov 국면 탐정, Turing 만능 학습), 합의 2종(Condorcet 빠른·최종)의 예측을 낸다. 4분 뒤 두 기준(봉 마감, 표시 시점)으로 채점한다. 결과는 `state/engine.sqlite3`에 쌓인다.
- 알고리즘은 모두 수습으로 시작한다. 매일 장 마감 10분 뒤 상태(수습, 정식, 벤치, 은퇴, 복제)를 점검하고, 정식 알고리즘만 합의에 들어간다([docs/engine-api.md](docs/engine-api.md#상태-관리-17)).
- 순위표(`/v1/leaderboard`)와 알고리즘 상세 통계(`/v1/algorithms/<이름>/stats`)를 낸다. 방향 정밀도, 커버리지, 기준 3종 대비 lift, 가상손익, Brier 점수, 보정 곡선을 신뢰구간과 함께 보여준다.
- 알고리즘은 녹화된 날은 녹화 이벤트(중간가 기준)로, 백필만 있는 날은 1분봉(종가 기준)으로 학습한다. 모델은 `state/models/`에 저장되고, 실행 중인 엔진은 30초마다 확인해 바뀐 모델로 바꾼다.

```bash
uv run krx-quant fit newton
uv run krx-quant fit gauss
uv run krx-quant fit granger
uv run krx-quant fit markov
uv run krx-quant fit turing
uv run krx-quant train                 # 밤 학습 한 번: KRX_OBJECT_STORE의 최근 날로 학습·검증·승격 목록
uv run krx-quant rollback granger      # 직전 모델로 되돌리기
```
- 서버에서는 수집기가 매 거래일 21:30에 밤 학습을 돌리고, 다음 거래일 08:00에 이긴 모델로 바꾼다([docs/operations.md](docs/operations.md)).
- 서버는 매일 06:00(KST)에 main의 새 커밋을 받아 스스로 다시 띄운다. 새 버전이 뜨지 않으면 이전 커밋으로 되돌리고 텔레그램으로 알린다(`deploy/auto-update.sh`).
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
```

서버 없이 엔진 API를 띄운다(#20). 지어낸 아침(2026-10-12 09:00–10:30, 3종목, KOSPI·선물, VI 한 번)을 실제 엔진으로 재생하고, 실제 API 코드로 답한다. 늘 같은 응답이다. 다만 `status.cpu`의 시간은 실제로 재서 실행마다 조금 다르다. 시계는 10:30에 멈춰 있고, `status.mode`는 `demo`다. 모든 알고리즘을 정식(`active`)으로 두어 합의도 나온다. 그날 07:30 매크로 읽기는 위험회피(`risk_off`)로 정해져 있다. KIS 상태(`status.kis`, 결측 피드 하나)와 Newton의 모델 이력도 가상 값으로 보인다. Fincept 화면의 엔진 주소를 `http://127.0.0.1:8090`으로 바꿔 붙인다. DB는 `state/demo.sqlite3`(`--db`)에 새로 만들고, 매크로 기록과 모델 이력은 그 옆 `state/demo-data/`(`macro/`, `state/models/`)에 새로 쓴다. 응답은 실제 엔진과 같은 스키마를 지킨다.

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
| `collect` | 실시간 녹화, 예측·채점, 엔진 API, 업로드, 백필, 밤 학습 일정. 멈출 때까지 돈다 | 필요 |
| `measure-subscriptions` | 세션당 실시간 구독 한도를 잰다. 수집기를 멈추고 실행한다 | 필요 |
| `backfill` | 빠진 1분봉을 한 번 채운다. 수집기를 멈추고 실행한다 | 필요 |
| `archive [--recheck]` | 녹화한 날을 합쳐 `KRX_OBJECT_STORE`에 올린다. `--recheck`는 모든 파일을 저장소와 비교한다 | 필요(쓰지는 않는다) |
| `replay <YYYY-MM-DD> [--db 경로]` | 녹화한 하루를 엔진으로 다시 돌려 별도 DB에 쓴다 | 없어도 된다 |
| `fit <알고리즘> [--days N]` | 최근 날들로 학습해 `state/models/`에 설치한다. 녹화된 날은 녹화 이벤트(중간가), 백필만 있는 날은 1분봉(종가)으로 | 없어도 된다 |
| `train [--day YYYY-MM-DD]` | 밤 학습 한 번. `--day`가 없으면 Object Store에 모든 종목의 백필 1분봉이나 녹화된 체결이 있는 가장 최근 날 | 없어도 된다 |
| `macro` | 오늘의 매크로 지표를 한 번 읽고 국면을 정해 `data/macro`에 기록한다(#8). 실패한 출처가 있으면 종료 코드 1 | 없어도 된다 |
| `rollback <알고리즘>` | 직전 모델로 되돌린다 | 없어도 된다 |
| `retire <알고리즘>`, `unretire <알고리즘>` | 은퇴시킨다, 은퇴를 풀어 수습으로 되돌린다 | 없어도 된다 |
| `test-alert` | 텔레그램에 `[테스트]` 메시지를 보낸다 | 필요(쓰지는 않는다) |
| `notify <글>` | 텔레그램에 `[알림] <글>`을 보낸다. `deploy/auto-update.sh`가 쓴다 | 없어도 된다 |
| `fake-kis [--port 18080] [--host 127.0.0.1] [--trades]` | 가짜 KIS 서버(아래 "키 없이 확인하기") | 없어도 된다 |
| `demo-engine [--port 8090] [--host 127.0.0.1] [--db state/demo.sqlite3]` | 데모 엔진 API(아래 "화면을 만들 때") | 없어도 된다 |

종료 코드: 0은 성공이다. 2는 설정 오류(`configuration error: ...`)나 잘못된 인자다. 1은 KIS 오류(`KIS error: ...`, Docker가 다시 띄운다)이거나 명령이 할 일을 못 한 경우다. 예: 업로드에 실패한 파티션, 학습할 1분봉 없음, 되돌릴 모델 없음, 텔레그램 전송 실패.

## 문서

- [docs/design.md](docs/design.md): 설계 (목표, 구조와 모듈 지도, 데이터, 알고리즘, 학습, 인프라, 개발 단계). 바뀐 결정은 각 절의 **바뀜**·**삭제됨** 표시에 있다
- [docs/kis-feeds.md](docs/kis-feeds.md): KIS 피드, 키, 한도, 최근월물 찾기
- [docs/events.md](docs/events.md): 녹화 데이터와 1분봉 형식
- [docs/engine-api.md](docs/engine-api.md): 엔진 API, 예측·채점 규칙, 저장 구조, 알고리즘 규약과 학습, 재생
- [docs/operations.md](docs/operations.md): 서버 개통, 설정 값 전체, 매일 일정, 밤 학습과 모델 교체, 자동 업데이트, 키 교체, 장애 대응과 알림, 저장 위치
- [docs/paid-data.md](docs/paid-data.md): 유료 데이터 조사

## 진행 상황

2026-10-10 기준이다. 이슈는 `gh issue list --state all`로 본다. 자세한 것은 [docs/design.md](docs/design.md) 11장에 있다.

| 단계 | 내용 | 상태 |
|---|---|---|
| P0 | 저장소 준비, 설계 문서 | 완료 |
| P1 | 수집기 | 거의 완료(#2–#6, #8, #30). 남은 것: 첫 거래일 확인(#7), ETF 장중 확인(#29), REST 대체 추가(#28) |
| P2 | 예측·채점 핵심 | 완료: #9–#19. 호가 피처를 쓰는 Turing은 녹화가 쌓인 뒤(#55) |
| P3 | Fincept 화면, 빌드 | 진행 중. 첫 화면과 개인 빌드(#20)는 됐다. 나머지(#21)가 남았다 |
| P4 | 밤 학습 | 진행 중. 상시 서버의 밤 학습과 08:00 교체(PR #45), 녹화 이벤트로 학습하기(#22, PR #48)는 됐다. EC2 Spot과 비용 상한(#23)이 남았다 |
| P5 | 호가 알고리즘, 재생 테스트 | 하루 재생(`replay`)만 있다. Kalman(#25), Shannon(#26), Poisson(#27), 사전 검증 도구(#24) |

## 비밀값

API 키와 토큰은 서버의 `.env`에만 둔다. 저장소에 커밋하지 않는다.
