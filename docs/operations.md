# 운영 문서

상시 서버(AWS Lightsail 시드니)에 엔진을 띄우고 운영하는 방법이다. 설계는 [design.md](design.md) 9장에 있다.

- 서버: Lightsail 시드니(`ap-southeast-2`), 2GB, 공인 IPv4, Ubuntu 24.04. 엔진은 Docker(`compose.yaml`)로 돈다.
- 접속: Tailscale만 쓴다. 엔진 포트는 서버의 Tailscale 주소에만 열린다.
- 데이터: 녹화한 날은 S3에 올려 영구 보관한다. 서버는 매일 03:00 KST에 Lightsail 자동 스냅샷을 뜬다.
- 비밀값: 서버의 `.env`(권한 600)에만 둔다. 저장소에는 절대 커밋하지 않는다.

## 1. 사용자가 직접 할 일

계정 생성, 결제, 키 발급은 사람이 해야 한다. 이 순서대로 한다.

1. **AWS 계정**
   1. AWS에 가입하고 결제 수단을 등록한다. 루트 계정에 MFA를 켠다.
   2. 관리용 IAM 사용자(또는 IAM Identity Center 사용자)를 만든다. 루트 계정 키는 만들지 않는다.
   3. 맥에 AWS CLI를 설치하고(`brew install awscli`) 관리용 자격 증명으로 설정한다. 리전은 `ap-southeast-2`다.
      ```bash
      aws configure
      ```

   AWS의 새 가입 방식(AWS Settings 프로젝트)으로 만든 계정은 프로젝트마다 리전이 하나뿐이다. 연락처 주소의 국가에 따라 오하이오(`us-east-2`), 스톡홀름(`eu-north-1`), 시드니(`ap-southeast-2`) 중 하나로 자동으로 정해진다. 다른 리전 호출은 AWS가 관리하는 조직의 서비스 제어 정책이 막는다. 서울(`ap-northeast-2`)을 호출하면 `AccessDeniedException`(explicit deny in a service control policy)이 난다. 이 제한은 고급 기능을 활성화(Activate advanced features)해야 풀리는데, 되돌릴 수 없다. 그래서 시드니를 쓴다. 이유는 [design.md](design.md) 9장에 있다.

   이 프로젝트에는 월 $100 지출 한도도 걸려 있다. 한도에 닿으면 프로젝트 전체가 일시정지되고 자원이 멈춘다. 상시 서버도 멈춘다는 뜻이다. 조기 통제인 "Stop new resource launches"도 켜져 있다. 예상 비용이 월 $15 정도라서 지금은 그대로 둔다. 밤 학습을 EC2 Spot으로 옮기면(3장 "밤 학습을 EC2 Spot으로 옮기기") 서버의 비용 감시(Cost Guard)가 $100에서 Spot 학습만 끈다. 이 한도가 같은 $100이면 서버까지 멈추므로, 그때는 한도를 상한보다 높게(예: $130) 올릴지 정한다. 사용자가 정할 일이다.
2. **KIS Open API**: 실전계좌로 앱키와 앱시크릿을 발급받는다. 모의투자 키로는 통합 시세 피드가 오지 않는다.
3. **텔레그램 봇**
   1. BotFather에게 `/newbot`을 보내 봇을 만들고 토큰을 받는다.
   2. 만든 봇에게 아무 메시지나 보낸다.
   3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates`를 열어 `chat.id`를 확인한다.
4. **Tailscale**
   1. Tailscale에 가입한다. 맥과 윈도우에 앱을 설치하고 같은 계정으로 로그인한다.
   2. (선택) 서버를 브라우저 승인 없이 붙이려면 admin console → Settings → Keys에서 auth key를 만든다. 1회용으로, 만료는 짧게 둔다.
5. **매크로 API 키** (무료, 선택. #8)
   1. 한국은행 ECOS: https://ecos.bok.or.kr/api/ 에 회원가입하고 "인증키 신청"을 한다. 바로 나온다.
   2. FRED: https://fred.stlouisfed.org 에 계정을 만들고 https://fred.stlouisfed.org/docs/api/api_key.html 에서 키를 받는다.
   - 키가 없으면 그 출처(ECOS: 원/달러·국고채, FRED: 미국 10년물·WTI)만 건너뛴다. 국면은 Yahoo(SOX, VIX)와 달러선물 종가로도 정해진다.
6. **AWS 자원 만들기**: 2.1절의 스크립트를 실행한다.
7. **서버 개통**: 2.2절을 따라 한다.

## 2. 개통

### 2.1 AWS 자원 (맥에서)

버킷 이름은 전 세계에서 유일해야 한다(예: `krx-quant-<이름>`).

```bash
deploy/create-aws.sh krx-quant-<이름>
```

리전은 `AWS_REGION` 환경 변수, 없으면 `aws configure`에서 정한 리전(`ap-southeast-2`)을 쓴다. 모든 자원을 이 리전 하나에 만든다.

이 스크립트가 만드는 것이다. 다시 돌려도 이미 있는 자원은 그대로 둔다.
- S3 버킷: 시드니 리전에 만들고 공개 접근을 모두 막는다. 버전 관리를 켜서 덮어쓴 이전 버전을 30일 보관한다. 엔진은 지울 수 없지만 덮어쓸 수는 있어서, 잘못된 파일로 덮어써도 되돌릴 수 있게 하려는 것이다. 중간에 죽은 멀티파트 업로드는 7일 뒤 지운다(같은 수명 주기 규칙 `keep-replaced-versions-30-days`).
- IAM 사용자 `krx-quant-engine`: 이 버킷만 읽고 쓸 수 있다. 삭제 권한은 없다(`deploy/iam-policy.json`).
- Lightsail 인스턴스 `krx-quant`: Ubuntu 24.04다. 번들은 2GB, 공인 IPv4인 리눅스 번들을 자동으로 고른다(시드니는 `small_3_2`). 번들 ID가 리전마다 달라서다. `BUNDLE`로 직접 정할 수도 있다.
- Lightsail 방화벽: TCP 22는 Lightsail 브라우저 SSH에서만, UDP 41641(Tailscale 직접 연결)은 모두에게 연다. 나머지는 모두 닫는다.
- 자동 스냅샷: 매일 18:00 UTC(03:00 KST)에 뜬다. Lightsail은 최근 7개를 보관한다.

스크립트의 기본값은 환경 변수로 바꿀 수 있다: `AWS_REGION`(없으면 `aws configure`의 리전), `ZONE`(`<리전>a`), `INSTANCE`(`krx-quant`), `BUNDLE`(자동), `BLUEPRINT`(`ubuntu_24_04`), `IAM_USER`(`krx-quant-engine`), `SNAPSHOT_UTC`(`18:00`).

엔진이 쓸 액세스 키를 한 번만 만든다. 출력된 `AccessKeyId`와 `SecretAccessKey`는 서버 `.env`에 넣는다. 비밀 키는 이때만 보이므로 바로 옮긴다.

```bash
aws iam create-access-key --user-name krx-quant-engine
```

### 2.2 서버 준비

1. Lightsail 콘솔에서 `krx-quant` 인스턴스를 열고 **Connect using SSH**를 누른다.
2. 서버에서 KIS에 접속되는지 확인한다. 서버가 해외(시드니)에 있어서, KIS가 해외 IP를 막으면 엔진이 돌 수 없다.
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://openapi.koreainvestment.com:9443/oauth2/tokenP
   ```
   `000`이 아닌 코드가 나오면 접속된다. `000`이면 연결이 안 된 것이다. 개통을 멈추고 KIS에 해외 접속이 되는지 문의한다. WebSocket(`ops.koreainvestment.com:21000`)은 8단계의 엔진 로그로 확인한다.
3. 비공개 저장소를 받기 위해 읽기 전용 배포 키를 만든다.
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/krx-quant-deploy -N "" -C krx-quant-server
   cat ~/.ssh/krx-quant-deploy.pub
   ```
   출력된 공개 키를 GitHub `codeyoma/krx-quant` → Settings → Deploy keys에 추가한다. 쓰기 권한은 주지 않는다.
4. 저장소를 받는다. `fincept/` submodule은 서버에 필요 없다.
   ```bash
   printf 'Host github.com\n  IdentityFile ~/.ssh/krx-quant-deploy\n' >> ~/.ssh/config
   git clone git@github.com:codeyoma/krx-quant.git && cd krx-quant
   ```
5. 준비 스크립트를 실행한다. Tailscale 로그인 URL이 나오면 브라우저에서 승인한다. auth key가 있으면 `sudo TS_AUTHKEY=tskey-... deploy/setup-server.sh`로 실행한다.
   ```bash
   sudo deploy/setup-server.sh
   ```
   스크립트가 하는 일이다.
   - Docker, Tailscale(Tailscale SSH 포함), ufw를 설치한다.
   - Docker가 Tailscale 뒤에 시작되도록 순서를 정한다(`/etc/systemd/system/docker.service.d/after-tailscale.conf`). 부팅 때 Tailscale 주소가 아직 없어도 엔진 포트를 열 수 있게 `net.ipv4.ip_nonlocal_bind = 1`을 켠다(`/etc/sysctl.d/90-krx-quant.conf`). 이 둘은 처음 한 번만 하고, 그때만 Docker를 다시 띄운다.
   - 방화벽을 설정한다: 들어오는 연결은 Tailscale 인터페이스, 22번, 41641/udp만 허용한다.
   - 첫 실행에서는 `.env`를 `.env.example`에서 만들고(권한 600) 멈춘다.
   - 두 번째 실행에서는 `KRX_BIND_ADDRESS`를 쓰고, `data/`, `state/`의 주인을 이미지의 `engine` 사용자(uid 10001)로 바꾸고, 엔진을 띄우고, 자동 업데이트 타이머를 설치한다(4장).
   - auth key는 `TS_AUTHKEY` 환경 변수로 준다.
6. `.env`를 채운다.
   ```bash
   nano .env
   ```
   | 키 | 값 |
   |---|---|
   | `KIS_APP_KEY`, `KIS_APP_SECRET` | 1.2에서 받은 실전 앱키와 시크릿 |
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 1.3에서 받은 토큰과 chat id |
   | `KRX_OBJECT_STORE` | `s3://<버킷>/engine` |
   | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 2.1에서 만든 엔진용 키 |
   | `AWS_REGION` | `ap-southeast-2`. 버킷을 만든 리전과 같아야 한다 |
   | `KRX_SYMBOLS`, `KIS_WS_MAX_SUBSCRIPTIONS` | `.env.example`의 값 그대로(`005930,000660,009150`, `41`). 지우면 코드 기본값(`005930` 한 종목, 구독 한도 없음)이 된다 |
   | `KRX_BIND_ADDRESS` | 비워 둔다. 스크립트가 Tailscale 주소를 넣는다 |
   | `KRX_ECOS_API_KEY`, `KRX_FRED_API_KEY` | 1.5에서 받은 매크로 키(선택). 비워 두면 그 출처를 건너뛴다 |

   나머지 설정은 아래 "설정 값 전체"에 있다. `.env.example`에서 주석으로 둔 변수는 그대로 두면 기본값을 쓴다.
7. 스크립트를 한 번 더 실행한다. 이번에는 엔진을 빌드하고 띄운다.
   ```bash
   sudo deploy/setup-server.sh
   ```
8. 확인한다. 아래 명령은 docker 그룹이 적용되도록 SSH에 다시 접속한 뒤 실행한다.
   ```bash
   docker compose ps                  # engine이 running
   docker compose logs -f engine      # 구독 응답, "KIS rejected ..."가 없는지
   docker compose run --rm engine test-alert   # 텔레그램에 [테스트] 메시지가 오는지
   ```
9. 맥과 윈도우에서 Tailscale로 접속되는지 확인한다. 두 컴퓨터 모두 Tailscale 앱이 켜져 있어야 한다.
   - 맥(터미널):
     ```bash
     ssh ubuntu@krx-quant
     ```
   - 윈도우(PowerShell, Windows 10 이상에 OpenSSH 클라이언트가 들어 있다):
     ```powershell
     tailscale ping krx-quant
     ssh ubuntu@krx-quant
     ```
   Tailscale admin console에서 `krx-quant` 기기의 **Disable key expiry**를 켠다. 그래야 몇 달 뒤 서버가 tailnet에서 빠지지 않는다.
10. Tailscale SSH가 되면 공용 SSH(22번)를 닫는다. 맥에서 실행한다.
    ```bash
    aws lightsail put-instance-public-ports --instance-name krx-quant --region ap-southeast-2 \
      --port-infos '[{"fromPort": 41641, "toPort": 41641, "protocol": "udp", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]}]'
    ```
    서버에서도 닫는다.
    ```bash
    sudo ufw delete allow 22/tcp
    ```
    그다음부터 Lightsail 브라우저 SSH는 쓸 수 없다. 접속은 Tailscale로만 한다.

엔진 API는 맥과 윈도우에서 Tailscale로 읽는다. 공인 IP로는 열리지 않는다. 엔드포인트는 [engine-api.md](engine-api.md)에 있다.
```bash
curl http://krx-quant:8080/v1/status
curl http://krx-quant:8080/v1/symbols/005930/latest
curl http://krx-quant:8080/v1/macro
```
`/v1/status`의 `cpu`는 알고리즘이 CPU 예산을 얼마나 쓰는지다(`usage`가 1을 넘으면 늦게 등록된 알고리즘부터 건너뛴다). `/v1/macro`는 그날 07:30에 정한 국면과 지표다.

#### 설정 값 전체

엔진은 환경 변수와 `.env`에서 설정을 읽는다(`src/krx_quant/config.py`). 둘 다 있으면 환경 변수가 이긴다. 서버에서는 compose가 `.env`를 컨테이너 환경 변수로 넘기고, 아래 "compose" 값은 compose가 덮어쓴다.

- 변수가 없으면 기본값을 쓴다. 값이 틀리면 명령이 `configuration error: ...`를 내고 끝난다(종료 코드 2).
- 숫자는 따로 적지 않으면 0보다 커야 한다. 숫자 변수를 빈 값(`KRX_FEE_PERCENT=`)으로 두면 오류다.
- 문자열 변수 중 `KRX_OBJECT_STORE`, AWS 키, 텔레그램 값, 선물 코드, `KRX_S3_ENDPOINT_URL`은 빈 값이 "없음"이다.

| 변수 | 기본값 | 뜻과 범위 |
|---|---|---|
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 없음 | KIS 실전 앱키와 시크릿. `collect`, `backfill`, `archive`, `measure-subscriptions`, `test-alert`는 이 값이 없으면 시작하지 않는다 |
| `KIS_REST_URL` | `https://openapi.koreainvestment.com:9443` | KIS REST 주소. 모의투자 주소는 `.env.example`에 있다 |
| `KIS_WS_URL` | `ws://ops.koreainvestment.com:21000` | KIS WebSocket 주소 |
| `KIS_MASTER_URL` | `https://new.real.download.dws.co.kr/common/master` | 최근월물을 찾는 KIS 마스터 파일 주소 |
| `KIS_WS_MAX_SUBSCRIPTIONS` | 없음(KIS가 거절할 때까지 모두 구독) | 세션당 실시간 구독 수. 넘는 구독은 REST로 바꾸거나 뺀다. 1 이상. `.env.example`은 41 |
| `KIS_REST_MAX_PER_SECOND` | 20 | REST 초당 호출 한도. 수집기 안의 조회와 백필이 나눠 쓴다. 1–20 |
| `KRX_SYMBOLS` | `005930` | 예측하고 녹화할 종목(쉼표로 구분). 하나 이상 |
| `KRX_INDEX_CODES` | `0001,2001,0013` | 업종지수 코드. `0001`(KOSPI)이 꼭 있어야 한다(서킷브레이커 판정) |
| `KRX_US_SYMBOLS` | `NVDA,MU,AMD` | 미국 주간거래 티커. 나스닥이 아니면 `NYS:티커`, `AMS:티커`. 비워도 된다 |
| `KRX_ETF_SYMBOLS` | `133690,381180` | KRX 상장 미국 ETF. 비워도 된다 |
| `KRX_VENUE_TRADES` | `true` | KRX 전용·NXT 전용 체결도 구독한다. `1`, `true`, `yes`만 참이다. 끄면 Shannon의 `nxt_share`가 늘 값이 없다(#26). 구독 계획의 맨 끝이라 종목을 늘려 세션 한도에 걸리면 가장 먼저 잘린다 |
| `KRX_KOSPI200_FUTURES_CODE`, `KRX_USD_FUTURES_CODE` | 없음(마스터 파일에서 찾는다) | 최근월물 코드를 직접 정한다 |
| `KRX_OPTION_STRIKES_EACH_SIDE` | 2 | KOSPI200 옵션 ATM 위아래로 구독할 행사가 수. 0 이상 |
| `KRX_FOREIGN_ESTIMATE_POLL_SECONDS` | 300 | 외국인·기관 추정 가집계 조회 간격(초) |
| `KRX_VI_POLL_SECONDS` | 5 | 종목별 VI 현황 조회 간격(초). 시가 동시호가부터 장 마감까지 |
| `KRX_FALLBACK_POLL_SECONDS` | 5 | 한도로 잘린 피드의 REST 대체 조회 간격(초) |
| `KRX_HEALTH_CHECK_SECONDS` | 5 | 피드 지연(`stale`)을 판정하는 간격(초) |
| `KRX_RECOVERY_HOLD_SECONDS` | 60 | 재연결 뒤 이만큼 유지되어야 `[복구]` 알림을 보낸다(초) |
| `KRX_RECONNECT_INITIAL_SECONDS`, `KRX_RECONNECT_MAX_SECONDS` | 1, 60 | WebSocket 재연결 간격의 처음과 최대(초). 실패할 때마다 두 배로 늘린다 |
| `KRX_BACKFILL_DAYS` | 365 | 1분봉을 채우는 기간(일) |
| `KRX_BACKFILL_MAX_PER_SECOND` | 8 | 백필 조회 속도(초당). 20 이하 |
| `KRX_DATA_DIR` | `data` (compose `/data`) | Parquet 녹화 위치. 서버에서는 저장소의 `data/` |
| `KRX_STATE_DIR` | `state` (compose `/state`) | 토큰, 거래 일정, DB, 모델. 서버에서는 저장소의 `state/` |
| `KRX_FLUSH_ROWS`, `KRX_FLUSH_SECONDS` | 5000, 30 | 파티션마다 이 행 수나 초가 차면 part 파일을 쓴다 |
| `KRX_OBJECT_STORE` | 없음 | `s3://<버킷>/<접두어>` 또는 로컬 폴더. 없으면 장 마감 업로드와 밤 학습을 하지 않는다 |
| `AWS_REGION` | `AWS_DEFAULT_REGION`, 그것도 없으면 `ap-southeast-2` | 버킷 리전 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 없음(boto3가 스스로 찾는다) | 엔진용 IAM 키 |
| `KRX_S3_ENDPOINT_URL` | 없음 | S3 호환 저장소(MinIO 등) 주소 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 없음(로그만 남긴다) | 알림을 받을 봇과 대화 |
| `TELEGRAM_API_URL` | `https://api.telegram.org` | 텔레그램 Bot API 주소. 테스트가 가짜 서버로 바꾼다 |
| `KRX_FEE_PERCENT` | 0.015 | 편도 수수료율(%). 0–1. 보합 범위와 가상손익에 쓴다 |
| `KRX_API_HOST` | `127.0.0.1` (compose `0.0.0.0`) | 엔진 API가 듣는 주소 |
| `KRX_API_PORT` | 8080 (compose 8080) | 엔진 API 포트. 1–65535 |
| `KRX_ALGORITHM_SECONDS` | 10 | 알고리즘 하나가 봉 마감 한 번에 쓸 수 있는 시간(초). 55 이하이고 `KRX_CPU_BUDGET_SECONDS`보다 작아야 한다 |
| `KRX_CPU_BUDGET_SECONDS` | 30 | 봉 마감 한 번에 알고리즘 예상 시간 합의 상한(초) |
| `KRX_MIN_SAMPLE` | 200 | 순위와 상태 판정에 필요한 채점 수와 기준 예측과의 짝 수 |
| `KRX_PROBATION_REVIEW_DAYS` | `10,20,40` | 수습을 판정하는 날(수습 며칠째, 올라가는 정수, 400 이하). 마지막 날이 지나면 은퇴 후보 알림(#87) |
| `KRX_BENCH_REVIEW_DAYS` | `20,40,60` | 벤치에서 회복을 판정하는 날(같은 형식). 마지막 날이 지나면 은퇴 후보 알림. 재생(`replay`, `prevalidate`)도 같은 설정을 쓴다 |
| `KRX_REVIEW_CONFIDENCE` | 0.983 | 그 판정에 쓰는 거래일 단위 lift 구간의 신뢰수준(양쪽). 0.5 이상 0.9999 이하의 유한한 수. 판정 횟수가 늘면 높인다(본페로니) |
| `KRX_TRAIN_AT` | `21:30` | 거래일 밤 학습 시각(KST, `HH:MM`). `off`나 빈 값이면 하지 않는다. `KRX_TRAIN_ON=spot`이면 Spot 스케줄(`deploy/training.yaml`의 `TrainAt`)과 같게 둔다 |
| `KRX_TRAIN_ON` | `server` | 밤 학습을 돌리는 곳(#23). `server`는 이 서버의 별도 프로세스, `spot`은 AWS가 같은 시각에 띄우는 EC2 Spot이다. `spot`이면 서버는 학습에 필요한 설정과 휴장일 달력을 S3(`training/`)에 올리고, 결과를 다음 거래일 07:30까지 기다려 요약을 텔레그램으로 보낸다 |
| `KRX_COST_BUDGET` | (없음) | 서버가 1시간마다 읽는 AWS Budgets 예산 이름(`krx-quant-monthly`, #23). 이달 비용이 예산의 50%와 80%를 넘으면 한 번씩 알리고, 100%에 닿으면 `KRX_TRAINING_SCHEDULES`를 그달 동안 끈다. 없으면 보지 않는다. 있으면 `KRX_TRAINING_SCHEDULES`도 있어야 한다(없으면 설정 오류) |
| `KRX_TRAINING_SCHEDULES` | (없음) | 상한에서 끄고 다음 달 1일에 다시 켤 EventBridge Scheduler 스케줄(쉼표로 구분, `krx-quant-training,krx-quant-training-resume`) |
| `KRX_COST_CHECK_SECONDS` | 3600 | 비용을 읽는 간격(초). 60 이상 |
| `KRX_IMAGE_REPOSITORY` | (없음) | Spot 학습용 이미지를 올릴 ECR 저장소 URI. 엔진은 읽지 않고 `deploy/push-image.sh`(자동 업데이트가 부른다)만 읽는다. 없으면 올리지 않는다 |
| `KRX_RESTART_AT` | `07:20` | 수집기가 시작한 날 다음 거래일의 이 시각(KST)에 스스로 멈추면 Docker가 다시 띄운다(#115). 새 수집기가 그날의 선물·옵션 월물과 등가격 행사가를 다시 고른다. `off`면 하지 않는다. `KRX_MACRO_AT`과 08:00(NXT 장전) 전이어야 한다. 매크로를 읽은 뒤 다시 띄우면 새 수집기가 그날 아침을 다시 읽어, 그때 실패한 출처가 앞서 정한 국면을 하루 내내 덮기 때문이다 |
| `KRX_MACRO_AT` | `07:30` | 거래일 매크로 지표를 읽고 국면을 정하는 시각(KST). `off`나 빈 값이면 하지 않는다. 개장 전이어야 한다. 개장 이후 시각이면 읽지 않고 `macro read skipped` 경고를 남긴다 |
| `KRX_ECOS_API_KEY` | (없음) | 한국은행 ECOS 키. https://ecos.bok.or.kr/api/ 에서 회원가입 후 "인증키 신청"으로 받는다(무료, 즉시). 없으면 ECOS 지표(원/달러, 국고채 3년)를 건너뛴다 |
| `KRX_FRED_API_KEY` | (없음) | FRED 키. https://fred.stlouisfed.org/docs/api/api_key.html 에서 계정을 만든 뒤 받는다(무료). 없으면 FRED 지표(미국 10년물, WTI)를 건너뛴다 |
| `KRX_ECOS_URL`, `KRX_FRED_URL`, `KRX_YAHOO_URL` | 각 공식 주소 | 매크로 출처 주소. 테스트용이다 |
| `KRX_REGIME_SHOCK_SOX_PERCENT`, `KRX_REGIME_SHOCK_VIX`, `KRX_REGIME_SHOCK_USDKRW_PERCENT`, `KRX_REGIME_SOX_MOVE_PERCENT`, `KRX_REGIME_WEAK_WON_PERCENT` | `3`, `30`, `1`, `1`, `0.5` | 일일 국면 문턱값([design.md](design.md#45-매크로-지표)) |
| `KRX_SIGNAL_ALERT` | `off` | `on`이면 텔레그램 신호 알림을 보낸다([engine-api.md](engine-api.md#텔레그램-신호-알림-19)). VI·서킷브레이커, 기준가 없음, 목표 시각 지남이면 보내지 않는다. 보낸·막힌 신호는 DB `runs`에 `signal_sent`, `signal_blocked`로 남는다 |
| `KRX_SIGNAL_MIN_PROBABILITY` | `0.6` | 신호를 보낼 최종 합의의 상승(하락) 확률. 0.5–1. 알림이 꺼져 있어도 값을 검사한다 |
| `KRX_SIGNAL_MIN_AGREE` | `2` | 같은 방향을 예측해야 하는 정식 알고리즘 수. 1 이상 |

> **바뀜** (2026-10-12, #73): `KRX_ALGORITHM_SECONDS` 기본 30 → 10, `KRX_CPU_BUDGET_SECONDS`보다 작아야 한다. 이유: 둘이 같으면 한도를 다 쓰는 알고리즘 하나 때문에 뒤에 등록된 알고리즘이 모두 건너뛰어졌다(#71). `.env`에 30을 적어 두었다면 지우거나 30보다 작게 고쳐야 엔진이 뜬다.

> **바뀜** (2026-10-12, #116): 텔레그램 신호는 최종 합의의 확률과 정식 알고리즘의 동의만 맞으면 보냈고, 엔진을 다시 시작하면 같은 신호가 4분 안에 다시 갈 수 있었다 → VI·서킷브레이커, 기준가 없음, 목표 시각 지남이면 보내지 않고, 보낸·막힌 신호를 `runs`에 남기며, 재시작해도 4분 안에 다시 보내지 않는다. 이유: 매매하면 안 되는 때에도 신호가 나갔다(#78, #80).

compose와 스크립트가 읽는 변수다. 엔진은 읽지 않는다.

| 변수 | 읽는 곳 | 기본값 | 뜻 |
|---|---|---|---|
| `KRX_BIND_ADDRESS` | `compose.yaml` | 없음(없으면 compose가 멈춘다) | 엔진 포트를 열 서버의 Tailscale IPv4. `deploy/setup-server.sh`가 쓴다 |
| `KRX_ENGINE_PORT` | `compose.yaml`, `deploy/setup-server.sh`, `deploy/smoke-local.sh` | 8080 | 엔진 API를 여는 서버 쪽 포트. 컨테이너 안은 늘 8080 |
| `TS_AUTHKEY` | `deploy/setup-server.sh` | 없음(브라우저 로그인) | Tailscale auth key |
| `KRX_UPDATE_TRIES`, `KRX_UPDATE_PAUSE` | `deploy/auto-update.sh` | 36, 5 | 새 엔진이 API에 답하는지 이 횟수만큼, 이 초 간격으로 본다(기본 3분) |
| `KRX_UPDATE_NOW` | `deploy/auto-update.sh` | 지금(KST) | `<요일 1–7> <HHMM>`. 장 시간 판정을 테스트에서 바꾼다 |
| `AWS_REGION`, `ZONE`, `INSTANCE`, `BUNDLE`, `BLUEPRINT`, `IAM_USER`, `SNAPSHOT_UTC` | `deploy/create-aws.sh` | 2.1절 | AWS 자원 이름과 위치 |
| `KEEP`, `SECONDS_TO_RECORD` | `deploy/smoke-local.sh` | 없음, 15 | `KEEP=1`이면 컨테이너와 볼륨을 남긴다. 녹화 시간(초) |

Docker 이미지는 `TZ=Asia/Seoul`, `KRX_DATA_DIR=/data`, `KRX_STATE_DIR=/state`로 돈다(`Dockerfile`).

### 2.3 첫 거래일 확인 (#7)

1. **장 마감 후 S3에 올라갔는지** 맥에서 확인한다.
   ```bash
   aws s3 ls s3://<버킷>/engine/events/date=$(date +%F)/ --recursive
   ```
2. **실시간 구독 한도**를 잰다. 앱키 하나에 세션 하나라서 엔진을 멈추고 잰다. 장중이 아닐 때 한다.
   ```bash
   docker compose stop engine
   docker compose run --rm engine measure-subscriptions
   docker compose start engine
   ```
   결과가 41이 아니면 `.env`의 `KIS_WS_MAX_SUBSCRIPTIONS`를 그 값으로 고치고 `docker compose up -d`를 실행한다.
3. **종목별 메시지량**을 센다. 날짜를 바꿔 실행한다. 21:00 업로드가 끝난 뒤에 하면 파티션마다 파일 하나라 빠르다.
   ```bash
   docker compose run --rm --no-deps --entrypoint /app/.venv/bin/python engine -c '
   import pyarrow.dataset as ds
   for layer in ("raw", "events"):
       t = ds.dataset(f"/data/{layer}/date=2026-10-12", partitioning="hive").to_table(columns=["feed", "symbol"])
       for row in t.group_by(["feed", "symbol"]).aggregate([("feed", "count")]).sort_by("feed").to_pylist():
           print(layer, row["feed"], row["symbol"], row["feed_count"])'
   ```
4. **하루 데이터 크기**를 잰다.
   ```bash
   du -sh data/*/date=2026-10-12
   ```
5. 잰 값은 `docs/design.md` 13장(확인 필요 항목)에 적는다.
6. **REST 대체 조회**를 장중(09:00–15:20)에 한 번 확인한다(#28). 한도로 잘린 구독을 대신하는 REST 조회를 하나씩 한 번 부르고, 기록할 이벤트를 출력한다. REST만 써서 엔진을 멈추지 않아도 된다.
   ```bash
   docker compose run --rm engine check-fallbacks
   ```
   - 마지막 줄이 `N of N fallbacks answered`이고 종료 코드가 0인지 본다. 실패한 줄(`failed: ...`)은 그 조회와 이유를 보여 준다.
   - [kis-feeds.md](kis-feeds.md#rest-대체-조회)의 ⚠ 항목을 확인한다.
     - 달러선물 `CF` 줄이 실패하지 않는지 본다.
     - 옵션 줄의 `price`가 옵션 가격으로 보이는지 본다.
     - 프로그램매매 줄의 `net_value`를 같은 시각 `events/feed=H0UNPGM0`의 실시간 값과 견줘 금액 단위를 확인한다.
   - 확인한 내용은 kis-feeds.md에 적고 ⚠를 지운다.

### CPU 여유 재기

Lightsail은 버스트형 CPU다. 기준선보다 많이 쓰면 쌓아 둔 버스트 용량을 쓰고, 바닥나면 기준선으로 느려진다. 장중에(개장 직후와 오후에 한 번씩) 다음을 본다(#69).

1. **엔진이 보는 알고리즘 시간.** 맥에서 실행한다.
   ```bash
   curl -s http://krx-quant:8080/v1/status | python3 -c 'import json,sys; c=json.load(sys.stdin)["cpu"]; print(c["usage"], c["expected_seconds"], c["algorithms"])'
   ```
   `usage`는 봉 마감 한 번에 알고리즘들이 걸릴 것으로 보는 시간의 합을 CPU 예산(`KRX_CPU_BUDGET_SECONDS`)으로 나눈 값이다. 실제 걸린 시간(벽시계)이라 버스트 용량이 바닥나 CPU가 느려지면 함께 오른다.
2. **서버 CPU.** 서버에서 실행한다.
   ```bash
   docker stats --no-stream
   ```
3. **버스트 용량.** Lightsail 콘솔 → 인스턴스 → 지표에서 CPU 사용률과 남은 버스트 용량(Remaining CPU burst capacity, 분과 %)을 본다.
4. **판단.** 다음 중 하나라도 해당하면 CPU 기준선이 높은 요금제($44: 기준선 30%, $84: vCPU 4개 40%)나 비버스트형 EC2로 옮긴다([design.md](design.md) 9장). $24(4GB)는 $12와 CPU가 같아서(vCPU 2개, 기준선 20%) CPU 문제에는 소용없다. 메모리가 모자랄 때(`docker stats`의 메모리가 한도에 닿을 때)만 $24로 올린다.
   - `usage`가 0.5를 넘는다(잠정 기준: 예산의 절반이면 장중 부하가 몰릴 때 건너뜀이 생길 수 있다).
   - 버스트 용량이 장중 내내 줄기만 해서 바닥에 닿는다.
   - `[알림] <날짜> 예산 때문에 예측이 빠진 알고리즘이 있습니다.`가 온다.
5. 잰 값은 [design.md](design.md) 13장에 적는다.

### 진단 (#82)

남은 결정(상태 판정 기준, 스프레드, 원천 지연, 매크로 결측)에 필요한 실측을 한 번에 텍스트로 받는다.

- 엔진 DB는 쓰지 않고 읽는다. 엔진이 돌고 있으면 그 옆에서 읽기 전용으로 읽는다. 엔진이 멈춰 있으면 DB 옆에 파일(`-wal`, `-shm`)도 만들지 않는다.
- 녹화는 `data/`의 Parquet로 읽는다. 장 마감 업로드가 파티션을 합치는 중이면 파일 목록을 한 번 다시 읽는다.
- 그날 녹화가 다 모인 장 마감 뒤에 돌린다. 수집기가 도는 중에 돌려도 된다.
- 피드마다 읽고 버리며, 값은 float32 초로 들고 있어서 2GB 서버에서도 돈다.

```bash
docker compose run --rm engine diagnose --days 5
```

- `--days N`: 예측, 녹화, 매크로 중 하나라도 있는 최근 N거래일(시장 달력의 거래일). 기본 20이다.
- 분포는 p50, p90, p99와 최대로 보인다. 분위수는 정렬한 값에서 q·(n − 1) 위치의 아래쪽 값이다(값을 지어내지 않는다). 기준값으로 판정하지는 않는다.
- 데이터가 없는 절만 `없음`이다. 그래도 종료 코드는 0이다.

| 절 | 내용 |
|---|---|
| 1. 알고리즘별 표시 지연과 기준 A·B | `ok` 예측의 봉 마감 → 저장(`displayed_at`) 초. 채점된 예측 수와, 두 기준 모두 채점된 것 중 기준 A와 B의 클래스가 다른 비율 |
| 2. 채점된 예측과 예측 상태 | 채점된 예측 중 특수 상황 비율, 상태(`ok`, `vi`, `no_book`, `timed_out`, `error` 등)별 예측 수 |
| 3. 종목별 봉 마감 스프레드 | 통합 호가(`H0UNASP0`)만 본다. 봉 마감(09:01–15:16)마다 그 전 1분 안(NXT 개장 09:00:30부터)에 받은 마지막 유효 호가의 스프레드(틱)다. 기준가는 체결에 실린 호가도 쓰므로 더 늦은 호가를 볼 수 있다 |
| 4. 피드별 수신 지연 | 수신 시각 − 거래소 시각(초). 거래소 시각은 초 단위라 최대 1초 크게 나온다. 거래소 시각이 수신 초로 채워지는 행이 있는 피드는 줄 끝에 어떤 행인지 적는다: 업종지수 REST(모든 행), 업종지수(장 마감 기록), 선물·달러선물·옵션(REST 대체 행, 장 마감 기록), 미국 주간거래(REST 대체 행). VI 현황은 거래소 시각이 VI 시작이라 뺀다 |
| 5. 매크로 국면 | 거래일마다 개장 전에 정한 국면(엔진과 학습이 쓰는 것)을 본다. 이유가 `지표 없음`뿐인 날, 일부 지표가 빠진(행이 없거나 값이 비어 있는) 날과 빠진 지표, 국면 기록이 없는 날 |

첫 결과(2026-10-13 장 마감 뒤)와 그것으로 내린 결정은 [design.md](design.md) 13장 "진단 실측"에 있다. 다시 돌리면 그 값과 비교한다. 녹화가 20거래일쯤 쌓이거나 무거운 알고리즘이 들어온 뒤가 그때다. 장중 녹화가 여러 날 쌓일수록 3과 4의 값이 의미 있다.

> **바뀜** (2026-10-14, #157): 출력을 #90에 붙인다 → 첫 결과는 design.md 13장에 있고, 다시 돌린 결과는 그것과 비교한다. 이유: #90에서 결정을 내리고 실측을 문서에 옮겼다.

## 3. 매일 일어나는 일

| 시각 (KST) | 일 |
|---|---|
| 03:00 | Lightsail 자동 스냅샷 |
| 06:00 (매일) | 자동 업데이트(`krx-quant-update` 타이머). main에 새 커밋이 있으면 받아 엔진을 다시 띄운다(4장) |
| 07:20 (거래일) | 전날 이전부터 돌던 수집기가 스스로 멈추고 Docker가 다시 띄운다(`KRX_RESTART_AT`, #115). 그날의 KIS 마스터로 선물·옵션 월물과 등가격 ±2 행사가를 다시 고른다. 그날 06:00 배포로 시작한 수집기는 그대로다. 매크로(07:30)와 NXT 장전(08:00) 전이라, 새 수집기가 그날 매크로를 처음부터 읽고 녹화가 비지 않는다 |
| 07:30 (거래일) | 매크로 지표(ECOS, FRED, Yahoo, 달러선물 백필)를 읽고 그날 국면을 정해 `macro` 계층에 기록한다(`KRX_MACRO_AT`). 실패한 출처는 10분 뒤 다시, 그날 아침 3번까지 읽는다. 다시 읽기는 개장 전에만 한다 |
| 08:00 (거래일) | 아직 적용하지 않은 가장 최근 밤의 승격 목록대로 모델을 바꾼다. 바꾸거나, 목록을 쓰지 않으면 텔레그램으로 알린다 |
| 장중 | 실시간 녹화. 끊김과 지연은 `gaps`에 기록하고 텔레그램으로 알린다. VI 현황은 시가 동시호가부터 장 마감까지 5초마다, 외국인·기관 추정 가집계는 5분마다 조회한다 |
| 09:01–15:16 매분 | 기준 예측 3종, 알고리즘, 빠른·최종 합의가 예측하고, 4분 뒤 채점한다. `state/engine.sqlite3`에 쌓인다 |
| 장 마감 10분 뒤 (보통 15:40, 수능일 16:40) | 알고리즘 상태를 점검하고(수습·정식·벤치·복제), 합의 가중치를 다시 계산한다. 은퇴 후보가 있으면 텔레그램으로 알린다. 그날 시간 초과나 CPU 예산으로 빠진 예측이 있으면 알고리즘별 횟수를 알린다. 엔진이 시작한 뒤 첫 점검에서는 사전 검증을 기다리는 알고리즘도 알린다(#24) |
| 장 마감 15분 뒤 (보통 15:45, 수능일 16:45) | 그날 녹화를 파티션마다 합쳐 S3에 올린다 |
| 20:30 | 휴장일 정보를 갱신하고 1분봉 백필을 돌린 뒤, 그날 일봉과 새로 끝난 주·월·년봉을 더한다(`candles`, #111, #112). 처음에는 KIS에 있는 전체 기간을 받는다 |
| 21:00 | 다시 올린다. NXT·애프터마켓의 늦은 데이터와 그날 1분봉, 바뀐 일·주·월·년봉 파일(`candles/period=<기간>/symbol=<종목>/candles.parquet`)이 들어간다 |
| 21:30 (거래일) | 밤 학습(`KRX_TRAIN_AT`). 알고리즘마다 다시 학습하고, 이긴 모델만 승격 목록에 올린다. 결과를 텔레그램으로 보낸다. `KRX_TRAIN_ON=spot`이면 AWS가 같은 시각(평일)에 EC2 Spot을 띄우고, 00:30에 한 번 더 띄워 회수된 밤을 이어 간다. 서버는 결과를 기다려 요약을 보낸다 |
| 1시간마다 (`KRX_COST_BUDGET`이 있으면) | 이달 AWS 비용을 읽는다. 50%·80%에서 알리고, 100%에서 Spot 학습을 그달 동안 끈다. 다음 달 1일 첫 확인에서 다시 켠다 |
| 30초마다 | 모델 파일(`state/models/<알고리즘>.json`)이 바뀌었는지 본다(`fit`, `rollback`). 바뀌었으면 엔진이 새 모델로 바꾸고, 지금 도는 모델을 S3(`live.json`, `models/`)에 올린다 |

- 수집기가 시작할 때도 지난날 중 아직 안 올린 날을 올리고 백필과 일봉 갱신을 돌린다. 일·주·월·년봉의 첫 전체 수집은 종목마다 수십~수백 번 묻고, 백필과 같은 속도(`KRX_BACKFILL_MAX_PER_SECOND`)로 간다.
- 거래일 07:30부터 개장(09:00) 전 사이에 수집기가 시작하면 매크로 지표를 바로 한 번 읽는다. 개장 뒤에 시작하면 다음 거래일 07:30에 읽는다. 그날 국면이 장중에 바뀌지 않게 하려는 것이다. 손으로 읽으려면 `docker compose run --rm --no-deps engine macro`(이 명령은 언제든 새 줄을 기록한다).
- 수집기가 시작할 때 지난밤 학습 결과를 읽고, 지금 도는 모델을 S3에 올린다.
- 거래일에 08:00부터 개장 전 사이에 수집기가 시작하면, 그날 08:00 교체를 시작 30초 뒤에 바로 한다. 개장 뒤에 시작하면 다음 거래일 08:00에 한다.
- 엔진이 꺼져 있어서 상태 점검을 놓치면, 다시 켜질 때 놓친 날 것부터 돌린다. 밤 학습은 학습 시각 뒤 8시간 안에 다시 시작하면 바로 돈다(아래).
- 올리는 작업은 다시 돌려도 결과가 같다. 바뀐 파일만 다시 올린다.
- 손으로 돌리려면 `docker compose run --rm engine archive`를 실행한다.
- 업로드 기록은 서버의 각 파티션 `.uploaded.json`에 저장소별로 남는다. S3 쪽에서 객체가 사라졌을 수 있으면 `docker compose run --rm engine archive --recheck`로 모든 파일을 S3와 비교해 빠진 것을 올린다.

### 밤 학습과 모델 교체 (#22)

밤 학습은 기본으로 Lightsail 수집기가 21:30에 별도 프로세스로 띄운다(`KRX_TRAIN_ON=server`). EC2 Spot에서 돌릴 수도 있다(`spot`, 아래 "밤 학습을 EC2 Spot으로 옮기기"). 학습 내용과 S3에 남는 것은 어디서 돌든 같다.

> **바뀜** (2026-10-11, #67): "지금은 Lightsail 수집기가 띄운다(#23에서 EC2 Spot으로 옮긴다)" → 서버와 EC2 Spot 중에서 고른다(`KRX_TRAIN_ON`). 기본은 그대로 서버다. 이유: #23. Spot으로 옮기는 것은 AWS 비용이 드는 일이라 사용자가 스택을 배포하고 켠다.

- 알고리즘마다 학습에 안 쓴 최근 3거래일로 새 모델(challenger)과 서버에서 돌고 있는 모델(champion)을 비교한다. log loss가 더 낮을 때만 승격한다. 학습 구간은 그 앞의 알고리즘별 거래일 수(기본 20)다.
- 리포트에는 그 교체가 잡음인지 판단할 근거도 남는다(#89). 검증일별·기준가별 평균 손실, 두 모델이 모두 예측한 표본에서 짝지은 손실 차이(challenger − champion)의 평균과 그 평균의 표준오차(날 단위로 묶어 셈. 같은 날의 4분 정답은 서로 겹친다), champion이 검증일을 학습했는지, 학습한 코드의 커밋이다. 교체 규칙은 그대로 평균 log loss만 본다. 최소 개선폭과 champion이 검증일을 학습한 경우의 처리는 사용자가 정한 뒤 따로 한다.
- S3의 `bars/`(백필)와 `events/`(녹화), 그리고 날마다의 매크로 국면(`macro/.../feed=regime/`, Markov가 쓴다, #14)을 읽는다. 녹화된 날은 중간가로, 백필만 있는 날은 종가로 학습한다([engine-api.md](engine-api.md#학습)). 리포트의 `reference_prices`에 날짜별로 `mid` 또는 `close`가 남는다. 텔레그램 요약 첫 줄에는 날 수가 나온다. 예: `2026-10-13 밤 학습 (녹화 중간가 4일, 백필 종가 19일)`.
- 가장 긴 학습 구간(Turing 60거래일)에 검증 3일을 더한 날들을 읽는다(#16 전에는 23일). 그래서 캐시, 학습 시간, 메모리가 그만큼 는다. 날마다 모든 알고리즘의 피처를 한 번에 계산해 두기 때문이다. Turing 모델 파일은 1MB 안팎이다.
- 읽은 파일은 `state/training-cache/`에 복사해 두고 다음 밤에 다시 쓴다. 그 밤에 쓴 가장 오래된 날보다 오래된 날짜는 학습이 끝나면 지운다.
- S3에 남는 것:
  - `models/<알고리즘>/<버전>.json`: 모델
  - `reports/<날짜>/<알고리즘>.json`: 검증 리포트. 알고리즘 단위 체크포인트다. 학습이 중간에 죽어도 다시 돌리면 끝난 알고리즘은 건너뛴다. 이전 버전이 쓴 체크포인트(`commit`이 없다)는 근거 필드가 없어 다시 학습한다(#89).
  - `promotions/<날짜>.json`: 승격 목록
  - `live.json`: 서버가 지금 돌리는 버전. 다음 밤의 champion이다. 사전 검증을 기다리는 알고리즘(#24)의 설치된 모델도 들어간다.
- 08:00 교체는 아직 적용하지 않은 가장 최근 밤의 목록을, 그날 07:30까지 확정됐고 목록의 모델이 S3에 모두 있을 때만 쓴다. 아니면 전날 모델을 그대로 쓴다. 늦게 확정된 목록은 더 새 밤의 목록이 없으면 다음 날 아침에 쓴다.
  - 목록은 모두 쓰거나 하나도 쓰지 않는다. 모델이 하나라도 S3에 없으면 적용했다고 표시하지 않고, 다음 날 아침에 다시 시도한다.

  > **바뀜** (2026-10-10, #45): 08:00 교체 규칙이 PR 안에서 세 번 바뀌었다. 처음(75fa527)은 "07:30까지 확정된 목록만", 다음(ef08287)은 "그 밤 바로 다음 거래일 아침의 목록만, 늦은 목록은 쓰지 않음", 지금(1590d11)은 위 규칙이다. PR #45 본문은 두 번째 규칙을 적고 있으니 이 문서를 따른다. 이유: 이른 `KRX_TRAIN_AT`이나 늦게 끝난 학습의 목록도 다음 아침에 쓰고, 모델이 빠진 목록을 반만 적용하지 않기 위해서다(1590d11).
- `KRX_TRAIN_AT`을 21:00(그날 1분봉 업로드) 전으로 당기면 전 거래일까지의 데이터로 학습한다.
- `/v1/status`의 `training.applied`는 지금 그 승격 모델이 돌고 있는지다. 적용 뒤 `rollback`이나 `fit`으로 바꾸면 `false`가 되고, `reason`에 지금 도는 버전이 나온다.
- 수집기가 학습 시각 뒤 8시간 안에 다시 시작하면(배포, 장애) 그 밤의 학습을 바로 이어서 돌린다. 끝난 알고리즘은 건너뛴다. 학습이 6시간 안에 끝나지 않으면 멈추고 알린다.
- 손으로 학습한 모델(`fit`)이 champion이면 검증일까지 학습했을 수 있다. 그러면 리포트의 `reason`에 `trained on these days`가 붙고, 이기기가 실제보다 어렵다.
- 서버의 모델은 `state/models/<알고리즘>.json`이다. 설치한 버전은 `state/models/versions/`에, 언제 왜 바꿨는지는 `state/models/history.json`에 남는다.
- 상태는 `curl http://krx-quant:8080/v1/status`의 `training`(지난밤 날짜, 적용 여부, 알고리즘별 결과)과 `/v1/algorithms`의 모델 버전으로 본다.

```bash
docker compose run --rm --no-deps engine train            # 밤 학습을 지금 한 번 (끝난 밤이면 그대로 둔다)
docker compose exec engine krx-quant rollback granger     # 직전 모델로 되돌린다. 엔진이 1분 안에 바꾼다
```

- 되돌린 모델은 다음 승격까지 유지된다. 이미 적용한 승격 목록을 다시 적용하지 않는다.
- 밤 학습을 끄려면 `.env`에 `KRX_TRAIN_AT=off`를 넣고 `docker compose up -d`를 실행한다.
- 손으로 학습한 모델(`fit`)도 `history.json`에 남아서 되돌릴 수 있다.
- 특정 밤을 다시 돌리려면 `docker compose run --rm --no-deps engine train --day 2026-10-12`. 이미 끝난 밤이면 그 결과를 그대로 보여 주고 텔레그램으로 다시 보내지 않는다.

> **바뀜** (2026-10-10, #45): 모델을 바꾸려면 엔진을 재시작하고, 되돌리려면 `state/models/<이름>.json`을 지우고 재시작했다(기본 모델로만 돌아갔다) → 엔진이 30초마다 모델 파일을 보고 스스로 바꾼다. `krx-quant rollback <이름>`이 직전 모델로 되돌리고, 이력은 `state/models/history.json`과 `versions/`에 남는다. 이유: 08:00 교체와 명령 하나로 되돌리기(#22 요구사항)를 위해서다.

### 밤 학습을 EC2 Spot으로 옮기기 (#23)

밤 학습을 상시 서버 대신 EC2 Spot에서 돌린다. 녹화가 쌓이면 학습 메모리가 2GB 서버에 빠듯해지기 때문이다. 월 비용 상한도 함께 건다. AWS 자원은 `deploy/training.yaml`(CloudFormation) 하나에 있다.

> **바뀜** (2026-10-14, #158): Spot 학습을 켜는 절차 → 지금 계정에서는 쓰지 않는다. 밤 학습은 상시 서버에서 돈다(`KRX_TRAIN_ON=server`). 스택은 배포했다가 지웠다. 그래서 Cost Guard, ECR 이미지, 두 스케줄도 없다. 이유: AWS Settings 프로젝트 계정의 서비스 제어 정책이 Spot을 막는다. 2026-10-14 시험 실행(아래 6단계)에서 `RunInstances`가 `explicit deny in a service control policy`로 거부됐다. 거부 메시지를 풀어 보니(`aws sts decode-authorization-message`) 문 `DenySpotMarketType`(`ec2:InstanceMarketType` = `spot`)이었다([design.md](design.md) 8장). 아래 절차는 고급 기능을 활성화해 이 정책을 바꾼 계정에서만 끝까지 된다.

**구조**
- **시작.** EventBridge Scheduler가 평일 21:30(KST)에 시작 템플릿으로 one-time Spot 인스턴스(기본 `m6i.large`, 8GB)를 띄운다.
- **학습.** 인스턴스는 Amazon Linux 2023에서 ECR의 엔진 이미지를 받아 `krx-quant train --from-store`를 한 번 돌린다. 서버가 올린 설정(`training/settings.json`: 종목, 선행 시리즈, 수수료율)과 휴장일 달력(`training/market_calendar.json`)으로 학습한다. 결과는 서버에서 돌 때와 같이 S3에 남는다. 실행 로그는 `training/logs/<시각>.log`에 남는다.
- **종료.** 인스턴스는 학습이 끝나면 성공이든 실패든 스스로 종료한다(`shutdown`이 종료로 이어진다). 무엇이 멈춰도 4시간(`MaxMinutes`) 뒤에는 꺼진다. 디스크도 함께 지워진다.
- **재시도.** 02:00에 한 번 더 띄운다. 첫 실행이 늦어도 01:30에는 꺼지므로 둘이 겹치지 않는다. Spot이 회수되어 끊긴 밤은 끝난 알고리즘을 건너뛰고 이어서 한다. 끝난 밤이면 몇 분 안에 끝난다. 재시도도 06:00 전에 꺼지므로 07:30 기한 안에 끝난다. Spot 용량이 없으면 스케줄러가 1시간 안에 다섯 번까지 다시 시도한다. `TrainAt`, `ResumeAt`, `MaxMinutes`를 바꾸면 이 관계를 지킨다.
- **비밀값.** 인스턴스에는 비밀값이 없다. S3와 ECR은 인스턴스 역할로 읽고 쓴다. 텔레그램 요약은 서버가 S3의 결과를 보고 한 번 보낸다(재시작해도 다시 보내지 않는다, `state/spot_summarized.json`). 다음 거래일 07:30까지 결과가 없으면 `[장애] <날짜> 밤 학습이 실패했습니다: Spot 학습 결과가 ... 오지 않았습니다`를 보낸다. 서버가 그 사이에 재시작해도(06:00 자동 업데이트 포함) 07:30까지는 그 밤을 기다린다. 상한으로 Spot 학습이 꺼진 달에는 결과가 없어도 알리지 않는다.
- **이미지.** 서버의 06:00 자동 업데이트가 새 이미지를 빌드한 뒤 ECR에 올린다(`deploy/push-image.sh`). ECR에는 최근 3개만 남는다.
- **비용 상한.** AWS Budgets 예산(`krx-quant-monthly`, 월 $100)을 서버의 Cost Guard가 1시간마다 읽는다.
  - 이달 비용이 50%와 80%를 넘으면 텔레그램으로 한 번씩 알린다.
  - 100%에 닿으면 두 스케줄을 끄고 알린다. 실시간 예측은 계속한다. 그달에는 확인할 때마다 꺼져 있는지 본다. 스택을 다시 배포해 켜져도 다시 끈다.
  - 다음 달 1일(UTC) 첫 확인에서 스케줄을 다시 켜고 알린다. 상태 파일을 잃었어도 켠다.
  - AWS는 예산의 비용을 하루에 몇 번만 새로 계산하므로, 몇 시간 늦게 반응할 수 있다. 월초에는 몇 시간 동안 지난달 값이 남아 있는데, AWS는 그것이 언제 계산한 값인지 알려 주지 않는다(`LastUpdatedTime`은 예산 자체를 고친 시각이다). 그래서 지난달 마지막으로 읽은 값 이상이 보이는 동안은 지난달 값으로 보고 알리거나 끄지 않는다. 그보다 낮은 값이 한 번 보이면 그때부터 이달 값이다.
  - 상한으로 끈 달에 이달 비용이 상한 아래로 보이면 다시 켠다. 한 달의 비용은 줄지 않으므로, 그 정지는 지난달 값으로 한 것이다(상태 파일을 잃은 채 월초를 맞은 경우).
  - 비용을 읽지 못하면(권한, 연결) `[장애] AWS 비용을 읽지 못해 상한을 지킬 수 없습니다`를 보내고, 다시 읽히면 `[복구]`를 보낸다.
- **권한.** 스택은 서버의 IAM 사용자(`krx-quant-engine`)에 정책 하나를 붙인다. 이미지 올리기, 예산 읽기, 두 스케줄 끄고 켜기만 할 수 있다.
- **예상 비용.** Spot `m6i.large`는 시드니에서 시간당 몇 센트다. 스케줄은 평일마다 두 번 띄운다(재시도는 끝난 밤이면 몇 분). 학습이 하루 30분 안팎이면 월 $1 안팎이다(미확인 ⚠, 첫 달에 Cost Explorer로 확인한다). ECR 저장은 월 $0.1 안팎이다.

**옮기는 순서** (맥에서, 관리용 자격 증명으로)
1. 비용 상한과 프로젝트 지출 한도를 정한다(1장). 같은 $100이면 상한에서 서버까지 멈춘다.
2. 리전에 기본 VPC가 있는지 본다. 인스턴스는 기본 VPC의 기본 서브넷에 뜬다. 없으면 만든다.
   ```bash
   aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[].VpcId'
   aws ec2 create-default-vpc   # 위 결과가 비어 있을 때만
   ```
3. 스택을 배포하고 출력값을 본다. `Bucket`은 `deploy/create-aws.sh`의 버킷이다.
   ```bash
   aws cloudformation deploy --stack-name krx-quant-training --template-file deploy/training.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides Bucket=<버킷>
   aws cloudformation describe-stacks --stack-name krx-quant-training --query 'Stacks[0].Outputs'
   ```
4. 서버의 `.env`에 출력값을 넣는다. 장 시간 밖에 한다.
   ```bash
   KRX_IMAGE_REPOSITORY=<ImageRepository>
   KRX_TRAIN_ON=spot
   KRX_COST_BUDGET=krx-quant-monthly
   KRX_TRAINING_SCHEDULES=krx-quant-training,krx-quant-training-resume
   ```
5. 서버에서 첫 이미지를 올리고 엔진을 다시 띄운다.
   ```bash
   deploy/push-image.sh
   docker compose up -d
   ```
6. 맥에서 한 번 띄워 본다. 이미 끝난 밤이면 몇 분 안에 끝나고 인스턴스가 사라진다.
   ```bash
   aws ec2 run-instances --launch-template 'LaunchTemplateName=krx-quant-training,Version=$Latest'
   aws ec2 describe-instances --filters Name=tag:Name,Values=krx-quant-training --query 'Reservations[].Instances[].State.Name'
   aws s3 ls s3://<버킷>/engine/training/logs/
   ```
   로그 마지막 줄이 `krx-quant training: exit 0`인지 본다. 처음 Spot을 띄우는 계정이면 Spot 서비스 연결 역할이 이때 만들어진다.
7. 다음 거래일 밤에 확인한다(#23의 마지막 조건).
   - 21:30 뒤 텔레그램에 `[알림] <날짜> 밤 학습` 요약이 오는지 본다. 서버가 S3에서 결과를 보고 보낸다.
   - 다음 날 08:00에 `[알림] 모델 교체`가 오거나, `/v1/status`의 `training.day`가 그 밤인지 본다.

**GPU Spot** (신경망 알고리즘이 생기면)
- 새 계정의 GPU Spot 한도는 0 vCPU다. "All G and VT Spot Instance Requests" 할당량(`L-3819A6DF`)을 보고, 0이면 늘려 달라고 요청한다. 승인에 하루 이상 걸릴 수 있다.
  ```bash
  aws service-quotas get-service-quota --service-code ec2 --quota-code L-3819A6DF --query Quota.Value
  aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-3819A6DF --desired-value 4
  ```
- 승인되면 `InstanceType=g4dn.xlarge`로 스택을 다시 배포한다. 그때 시작 템플릿의 이미지를 GPU 드라이버가 있는 것(Deep Learning AMI)으로 바꾼다. 지금 알고리즘은 모두 CPU로 학습한다.

**되돌리기와 지우기**
- 서버 학습으로 돌아가려면 `.env`의 `KRX_TRAIN_ON=server`로 바꾸고 `docker compose up -d`를 실행한다. 두 스케줄도 끈다.
  ```bash
  for s in krx-quant-training krx-quant-training-resume; do aws scheduler get-schedule --name "$s" --query State; done
  ```
  끌 때는 콘솔(EventBridge > Schedules)에서 Disable 하거나 스택을 지운다.
- 스택을 지우면 ECR 이미지까지 지워진다. 그 전에 `.env`에서 `KRX_TRAIN_ON`, `KRX_COST_BUDGET`, `KRX_TRAINING_SCHEDULES`, `KRX_IMAGE_REPOSITORY`를 뺀다.
  ```bash
  aws cloudformation delete-stack --stack-name krx-quant-training
  ```

### 새 알고리즘 사전 검증 (#24)

새 알고리즘을 머지하면 06:00 자동 업데이트로 배포되지만 엔진은 그 알고리즘을 바로 돌리지 않는다. 그날 15:40 점검 때 `[알림] 사전 검증 대기: <이름>. …`이 한 번 온다. 그러면 장 시간 밖에서(저녁이나 주말) 다음을 실행한다.

```bash
docker compose run --rm --no-deps engine prevalidate <이름>
```

- 녹화된 최근 20거래일을 하루씩 재생한다. 날마다 그 전날까지로 다시 학습하고, 등록된 모든 알고리즘을 같이 돌린다. 하루 재생(`replay`)의 20배쯤 걸리고 CPU를 많이 쓴다. 장중에는 실시간 예측과 CPU를 다투므로 돌리지 않는다. 21:30 밤 학습과도 겹치지 않게 한다. 메모리 2GB 서버에서 엔진과 같이 돌므로 처음 한 번은 `docker stats`로 메모리를 본다.
- 출력은 검사 네 가지(`features`, `budget`, `correlation`, `stats`)의 결과다. 기준은 [engine-api.md](engine-api.md#사전-검증-24)에 있다.
- 통과(`passed`, 종료 코드 0)하면 엔진이 다음 봉 마감부터 수습으로 돌린다. 장 시간 밖이면 다음 거래일 첫 봉 마감부터다. 재시작은 필요 없다.
- 거부(`refused`, 종료 코드 1)되면 엔진은 계속 기다린다. 고친 커밋이 배포된 뒤 다시 돌린다.
- 결과는 `state/prevalidations/<이름>.json`(리포트)과 `state/prevalidation-<이름>.sqlite3`(재생한 예측)에 남는다. 실시간 DB와 순위표는 바뀌지 않는다.
- 녹화가 20거래일보다 적으면 있는 날만 쓴다. 10거래일보다 적으면 리포트는 남지만 통과하지 못한다(`days` 검사). 녹화는 2026-10-08에 시작했다.
- 첫 대상은 Kalman(#25), Shannon(#26), Poisson(#27)이다. 배포된 날부터 엔진이 시작할 때마다 `사전 검증 대기: kalman, shannon, poisson` 알림이 온다. 알고리즘마다 따로 돌린다. 녹화된 거래일이 10일이 된 날 21:00 업로드 뒤에 돌릴 수 있다(10-12부터 빠짐없이 녹화했다면 2026-10-23). 그 전에 돌리면 `days` 검사에서 거부된다. 녹화된 날 수는 `ls -d data/events/date=* | wc -l`로 본다(휴장일 폴더가 있으면 그만큼 뺀다).
- 통과는 그때의 코드에만 유효하다. 통과한 뒤 엔진이 돌리기 전에 그 알고리즘의 코드가 바뀌어 배포되면 다시 기다린다. 다시 돌린다.
- 같은 알고리즘을 동시에 두 번 돌리지 않는다. 같은 DB와 리포트 파일에 쓴다.

## 4. 업데이트 배포

**자동이다.** main에 머지하면 다음 06:00(KST)에 서버가 스스로 받아 다시 띄운다(`deploy/auto-update.sh`, systemd 타이머 `krx-quant-update`).

- 06:00은 밤 학습(21:30) 뒤, NXT 개장과 아침 모델 교체(08:00) 전이다. 다시 띄우는 동안 녹화가 비지 않는다.
- 엔진이 마지막으로 정상 기동을 확인한 커밋은 서버의 `.auto-update-deployed`에 남는다. main이 그 커밋이면 아무것도 하지 않는다. 재부팅 등으로 업데이트가 중간에 끊겼으면 다음 실행이 다시 한다.
- 평일 07:50–20:10에는 손으로 실행해도 거부한다.
- 새 엔진이 3분 안에 API에 답하지 않으면 이전 커밋으로 되돌려 다시 띄운다. 실패한 커밋은 서버의 `.auto-update-failed`에 남아서, main이 더 바뀔 때까지 다시 시도하지 않는다.
- 결과는 텔레그램으로 온다(`krx-quant notify`): `[알림] 자동 업데이트: <이전> → <새 커밋>`과 커밋 제목(최대 10개), `[알림] 자동 업데이트 실패: …`, 그 밖의 오류(GitHub 접속 실패 등)는 `[알림] 자동 업데이트 오류: …`. 오류일 때 엔진은 그대로 돈다.
- 업데이트가 끝나면 쓰지 않는 Docker 이미지를 지운다.
- 이미지를 만들 때 그 커밋을 넘긴다(`KRX_COMMIT`, `compose.yaml`의 빌드 인자). 엔진은 `/v1/status`의 `commit`으로 보이고, 시작할 때마다 DB `runs`에 `start`로 커밋과 수수료율, 신호 규칙, 시간·CPU 예산을 남긴다(#85). 손으로 `docker compose up -d --build`하면 `unknown`이다. 커밋을 남기려면 `KRX_COMMIT=$(git rev-parse HEAD) docker compose up -d --build`로 띄운다.

  > **바뀜** (2026-10-13, #121): 도는 엔진의 커밋을 알 수 없었다 → 자동 배포가 이미지에 넘기고, 엔진이 보이고 기록한다. 이유: 성적을 코드와 설정별로 가를 수 없었다(#78, #85). 이 변경을 처음 받는 06:00 배포는 이전 스크립트로 이미지를 만들어 `unknown`이고, 그다음 배포부터 커밋이 나온다.
- **주의: SQLite 스키마를 올리는 main.** 엔진은 시작할 때 `state/engine.sqlite3`를 새 스키마로 올린다. 그 main이 API 확인에 실패하면 되돌린 옛 엔진은 새 스키마의 DB를 거부하고(`... was written by a newer engine (schema N)`) 뜨지 못한다. 그러면 `자동 업데이트 실패: ...되돌렸지만 엔진이 응답하지 않습니다`가 온다. 고친 커밋을 main에 올리고 장 시간 밖에서 `deploy/auto-update.sh`를 실행한다. 실패한 커밋만 건너뛰므로 더 새 main은 바로 시도한다.

```bash
journalctl -u krx-quant-update -n 50       # 지난 실행 기록
systemctl list-timers krx-quant-update     # 다음 실행 시각
deploy/auto-update.sh                      # 지금 바로 (장 시간 밖에서)
sudo systemctl disable --now krx-quant-update.timer   # 자동 업데이트 끄기
```

- 타이머는 `deploy/setup-server.sh`가 설치한다. 이미 개통한 서버에는 한 번만 `sudo deploy/install-auto-update.sh`를 실행한다.
  - `/etc/systemd/system/krx-quant-update.service`: `Type=oneshot`, 저장소 주인(`SUDO_USER`, 없으면 `ubuntu`)으로 저장소에서 `deploy/auto-update.sh`를 실행한다. Docker와 네트워크 뒤에 돈다.
  - `/etc/systemd/system/krx-quant-update.timer`: `OnCalendar=*-*-* 06:00:00 Asia/Seoul`, `Persistent=false`. 서버가 꺼져 있어서 놓친 실행은 부팅 때 돌리지 않는다. 부팅이 장 시간일 수 있어서다.
- 손으로 배포하려면 장 시간 밖에서 `git pull && docker compose up -d --build`를 실행한다. 그러면 `.auto-update-deployed`가 옛 커밋을 가리켜서, 다음 06:00 실행은 그 커밋을 새 커밋으로 보고 빌드와 API 확인을 한 번 더 한 뒤 알린다.

> **바뀜** (2026-10-10, #46): main에 머지할 때마다 사람이 서버에 들어가 `git pull && docker compose up -d --build`를 실행했다(장 마감 뒤, 21:00 업로드가 끝난 뒤가 좋다고 적었다) → 매일 06:00 타이머가 한다. 이유: 밤 학습(21:30)과 08:00 교체가 이미 자동이라 배포도 자동으로 했다(PR #46). 처음 버전(7aa438d)은 "체크아웃이 이미 main이면 아무것도 하지 않음"이었고, a886368에서 `.auto-update-deployed`로 바꿨다. 이유: 재부팅으로 중간에 끊긴 실행을 다시 하기 위해서다.

## 5. 키 교체

`.env`를 고친 뒤에는 `docker compose up -d`를 실행한다. 바뀐 설정으로 컨테이너를 다시 만든다. 장중에는 피한다.

| 키 | 교체 방법 |
|---|---|
| KIS 앱키·시크릿 | KIS에서 다시 발급한다 → `.env` → `docker compose up -d`. 캐시된 접근 토큰(`state/kis_token.json`)은 앱키가 바뀌면 자동으로 버리고 새로 받는다. |
| 텔레그램 봇 토큰 | BotFather `/revoke`로 새 토큰을 받는다 → `.env` → `docker compose up -d` → `docker compose run --rm engine test-alert` |
| AWS 액세스 키 | 1. `aws iam create-access-key --user-name krx-quant-engine`으로 새 키를 만든다(사용자당 2개까지).<br>2. `.env`에 넣고 `docker compose up -d`를 실행한다.<br>3. `docker compose run --rm engine archive`로 업로드되는지 확인한다.<br>4. 옛 키를 지운다: `aws iam delete-access-key --user-name krx-quant-engine --access-key-id <옛 키 ID>` |
| Tailscale | 서버 기기의 key expiry를 꺼 두면 교체할 일이 없다. 다시 인증하려면 `sudo tailscale up --ssh --hostname=krx-quant --force-reauth` |

`.env` 권한이 바뀌었으면 `deploy/secure-env.sh`로 다시 600으로 맞춘다.

## 6. 장애 대응

먼저 상태와 로그를 본다.

```bash
docker compose ps
docker compose logs --tail 200 engine
```

| 증상 | 확인 | 조치 |
|---|---|---|
| `[장애] KIS WebSocket 연결이 끊겼습니다` | 로그의 `KIS WebSocket down (...)` 원인 | 1초에서 최대 60초 간격으로 스스로 재연결한다. 60초 넘게 붙어 있으면 `[복구]`가 온다. 오래 가면 KIS 점검 공지를 보고, 앱키가 유효한지 확인한다. |
| `[장애] 구독한 키가 모두 끊긴 피드: stale <TR>` | 그 피드(TR)의 구독·조회 키가 전부 기준 시간(`health.STALE_SECONDS`)보다 오래 조용하다. 구독 키가 잘못됐거나(KIS는 잘못된 키도 구독 성공으로 답한다. 예: #113의 만기 지난 옵션 월물), 파싱이나 연결이 고장 났다. 여러 피드가 함께 오면 연결 문제다. | 키 하나라도 데이터가 다시 오면 `[복구] 데이터가 다시 들어오는 피드: stale <TR>`가 온다. 키 하나만 조용한 것(거래정지, VI, 체결이 드문 옵션 행사가)은 알리지 않는다. `gaps` 계층과 `/v1/status`의 `kis.feeds[].gap`에만 남는다. |
| `[장애] 장 마감 데이터 업로드가 ... 실패했습니다` | 로그의 `could not archive ...` | 1. AWS 키, `KRX_OBJECT_STORE` 버킷 이름, IAM 정책을 확인한다.<br>2. 고친 뒤 `docker compose run --rm engine archive`를 돌린다. 다시 돌려도 안전하다.<br>3. 다음 업로드가 성공하면 `[복구]`가 온다. |
| `[장애] 엔진 API를 열지 못했습니다` | 로그의 `Engine API not started on ...` | 녹화와 예측은 계속한다. 다른 프로세스가 8080을 쓰는지 확인하고 `docker compose up -d`로 다시 띄운다. |
| 알고리즘을 다시 학습하고 싶다 | 밤 학습 결과는 텔레그램과 `/v1/status`의 `training` | 밤 학습이 매일 한다. 당장 바꾸려면 알고리즘마다(`newton`, `gauss`, `granger`, `markov`, `kalman`, `shannon`, `poisson`, `turing`) `docker compose run --rm --no-deps engine fit <이름>`. 엔진이 1분 안에 새 모델로 바꾼다. 되돌리려면 `docker compose exec engine krx-quant rollback <이름>`(위 "밤 학습과 모델 교체"). |
| `[알림] 사전 검증 대기: <이름>. 검증을 통과해야 돌기 시작합니다. ...` | `curl -s http://krx-quant:8080/v1/algorithms`에 그 이름이 없다 | 새로 등록된 알고리즘이 사전 검증을 기다린다. 장 시간 밖에서 `docker compose run --rm --no-deps engine prevalidate <이름>`(3장 "새 알고리즘 사전 검증"). 엔진이 다시 시작할 때마다 한 번 온다. |
| `[알림] <이름> 은퇴 후보: ...` | `curl -s http://krx-quant:8080/v1/algorithms`의 `status` 이력, `/v1/algorithms/<이름>/stats?period=20d` | 벤치 60거래일 동안 회복하지 못했다. 은퇴시키려면 `docker compose exec engine krx-quant retire <이름>`. 기록은 남고, 엔진은 다음 봉 마감부터 돌리지 않는다. 되돌리려면 `krx-quant unretire <이름>`(수습부터 다시). |
| `[알림] <날짜> 예산 때문에 예측이 빠진 알고리즘이 있습니다. ...`, 또는 `WARNING krx_quant.engine: <이름> skipped at ... for the CPU budget`나 `<이름> took over 10.0s` 로그 | `curl -s http://krx-quant:8080/v1/status`의 `cpu`(알고리즘별 예상 초와 `usage`), `docker stats`로 CPU 사용률, DB의 `runs` 테이블(`kind`가 `cpu_budget`) | 시간 초과는 그 알고리즘이 봉 마감 한 번에 `KRX_ALGORITHM_SECONDS`를 넘긴 것이고, CPU 예산 건너뜀은 알고리즘들의 합이 `KRX_CPU_BUDGET_SECONDS`를 넘은 것이다. 알고리즘을 가볍게 하거나 CPU 기준선이 높은 요금제로 옮긴다(위 "CPU 여유 재기"). 예산을 늘리려면 한도가 예산보다 작게 둘 다 고친다. 알림은 그날 일일 점검 때 한 번만 온다. |
| `[장애] 매크로 출처 <ecos/fred/yahoo/kis>가 3번 연속 실패했습니다: <이유>` | 로그의 `macro source ... failed` | 키가 틀렸으면(`ECOS INFO-100`, `FRED 400 ... api_key`) `.env`를 고친다. `HTTP 5xx`나 연결 실패는 출처 쪽 장애다. 다음 거래일 07:30에 다시 읽고, 응답하면 `[복구] 매크로 출처 …가 다시 응답합니다.`가 온다. 그동안 그 출처의 지표 없이 국면을 정한다. |
| `[장애] <날짜> 밤 학습이 실패했습니다: <이유>` | 로그의 `nightly training of ... failed`. 6시간 안에 안 끝나면 이유가 `not done in 6:00:00`이다 | `docker compose run --rm --no-deps engine train --day <날짜>`로 다시 돌린다. 끝난 알고리즘은 건너뛴다. 다음 밤 학습이 성공하면 `[복구] 밤 학습이 다시 됩니다.`가 온다. 그동안 전날 모델이 계속 돈다. |
| `[장애] <날짜> 밤 학습이 실패했습니다: Spot 학습 결과가 <다음 거래일> 07:30까지 오지 않았습니다` | S3의 `training/logs/`(최근 로그 마지막 줄), `aws ec2 describe-instances --filters Name=tag:Name,Values=krx-quant-training`, 스케줄러 상태 | Spot이 뜨지 못했거나(용량, 할당량, 상한으로 꺼진 스케줄) 학습이 실패했다. 맥에서 `aws ec2 run-instances --launch-template 'LaunchTemplateName=krx-quant-training,Version=$Latest'`로 다시 띄우면 끝난 알고리즘은 건너뛴다. 급하면 서버에서 `docker compose run --rm --no-deps engine train --day <날짜>`. 그동안 전날 모델이 계속 돈다. |
| `[알림] AWS 비용이 이달 $N로 상한 $100의 50%(80%)를 넘었습니다.` | AWS Billing > Cost Explorer | 할 일은 없다. 예상보다 빠르면 무엇이 비용을 쓰는지 본다. |
| `[알림] AWS 비용이 이달 $N로 상한 $100에 닿았습니다. 이달 남은 기간 밤 학습(Spot)을 끕니다. ...` | Cost Explorer | 그달은 학습 없이 전날 모델로 돈다. 서둘러 학습이 필요하면 서버에서 `train`을 손으로 돌린다. 다음 달 1일에 `[알림] 새 달(...)이라 밤 학습(Spot) 스케줄을 다시 켰습니다.`가 온다. |
| `[장애] AWS 비용을 읽지 못해 상한을 지킬 수 없습니다: <이유>` | 로그의 `could not check the AWS costs`, `.env`의 `KRX_COST_BUDGET`, IAM 정책(`krx-quant-engine-training`) | 예산 이름이나 권한이 틀렸거나 AWS에 닿지 않는다. 고치면 다음 확인에서 `[복구] AWS 비용을 다시 읽습니다.`가 온다. 그동안 상한이 지켜지지 않는다. |
| `[알림] 자동 업데이트: ...`에 `Spot 학습용 이미지를 올리지 못했습니다` | `deploy/push-image.sh`를 손으로 실행해 오류를 본다 | ECR 권한이나 연결 문제다. 다음 06:00에 다시 올린다. 그동안 Spot은 이전 이미지로 학습한다. |
| AWS 호출이 `... with an explicit deny in a service control policy`로 거부됨 | 서버 `.env`의 `AWS_REGION`, 맥이면 `aws configure get region` | AWS Settings 프로젝트 계정은 시드니(`ap-southeast-2`) 밖의 리전을 막는다(1장). `AWS_REGION=ap-southeast-2`로 고치고 `docker compose up -d`. 리전이 맞는데도 거부되면 계정 정책이 그 동작 자체를 막는 것이다(예: Spot, 8장). 메시지 끝의 `Encoded authorization failure message`를 맥에서 `aws sts decode-authorization-message`로 풀면 막은 문(`Sid`)이 보인다. 2026-10-14에 서버의 `AWS_REGION=ap-northeast-2`(서울)가 이것으로 ECR 로그인을 막았다(#23). |
| `[신호] <종목> 상승 64% · 정식 3개 중 2개 같은 방향 · 09:31 봉 마감, 09:35까지` | `/v1/symbols/<종목>/latest`의 `consensus-final` | `KRX_SIGNAL_ALERT=on`일 때만 온다. 장애가 아니라 예측 신호다. 같은 종목·방향은 4분 안에 다시 오지 않는다(엔진을 다시 시작해도). VI·서킷브레이커에 걸리거나, 기준가가 없거나, 목표 시각이 지난 신호는 오지 않고 로그에 `signal blocked (<이유>)`, DB `runs`에 `signal_blocked`로 남는다. 주문 코드는 없다. |
| `[알림] <날짜> 밤 학습`과 알고리즘별 `승격`·`유지`·`학습 안 함` | | 매일 밤 학습의 요약이다. 할 일은 없다. 승격된 모델은 다음 거래일 08:00에 바뀐다. |
| `[알림] 모델 교체 (<날짜> 밤 학습): <알고리즘> → <버전>` | `/v1/status`의 `training` | 08:00 교체가 됐다. 되돌리려면 `docker compose exec engine krx-quant rollback <이름>`. |
| `[알림] <날짜> 밤 학습 결과를 쓰지 않습니다: <이유>` | `/v1/status`의 `training.reason` | 전날 모델이 계속 돈다. `final at ..., after 07:30`이면 다음 아침에 쓴다(더 새 밤이 없으면). `not in the store: ...`면 S3에 모델이 없는 것이다. 다음 아침에 다시 시도한다. |
| `[알림] 자동 업데이트: <이전> → <새 커밋>` | | 06:00 자동 업데이트가 됐다. 할 일은 없다. |
| `[알림] 자동 업데이트 실패: <커밋>이 시작되지 않아 ...` | `journalctl -u krx-quant-update -n 50`, `docker compose logs --tail 200 engine` | 새 커밋이 3분 안에 API에 답하지 않아 이전 커밋으로 되돌렸다. 고친 커밋을 main에 올리면 다음 06:00에 받는다. "되돌렸지만 엔진이 응답하지 않습니다"면 4장의 SQLite 스키마 주의를 본다. |
| `[알림] 자동 업데이트 오류: <명령> (줄 N). 엔진은 그대로입니다.` | `journalctl -u krx-quant-update -n 50` | GitHub 접속 실패나 fast-forward가 안 되는 체크아웃 등이다. 엔진은 그대로 돈다. 원인을 고치고 다음 06:00을 기다리거나, 장 시간 밖에서 `deploy/auto-update.sh`를 실행한다. |
| 녹화된 날의 예측을 다시 보고 싶다 | | `docker compose run --rm --no-deps engine replay 2026-10-12`. 결과는 `state/replay-2026-10-12.sqlite3`다. 실시간 DB는 건드리지 않는다. |
| 엔진 컨테이너가 계속 재시작한다 | 로그의 `configuration error` 또는 `KIS error` | `.env` 값을 고치고 `docker compose up -d`를 실행한다(설정 값은 2.2의 "설정 값 전체"). |
| 엔진이 `... was written by a newer engine (schema N)`으로 시작하지 못한다 | 지금 커밋과 `.auto-update-deployed` | 옛 엔진이 새 엔진이 올린 DB를 읽었다. 엔진은 더 새 스키마의 DB를 거부한다. 그 스키마를 아는 커밋으로 배포한다(4장). |
| `EGW00133`(접근 토큰 발급 1분 1회) | 재시작을 반복했는지 | 토큰은 캐시되고, 1분 간격으로 스스로 다시 시도한다. |
| `EGW00201`(초당 거래건수 초과) | 백필과 REST 대체 조회가 겹쳤는지 | 0.5초 뒤 스스로 다시 보낸다. 계속되면 `KRX_BACKFILL_MAX_PER_SECOND`를 낮춘다. |
| 로그의 `no macro regime for <날짜>` | `docker compose logs engine \| grep macro`, `/v1/macro` | 그날 개장 전에 정한 매크로 국면이 없다(07:30 수집이 꺼졌거나 모든 출처가 실패했거나, 수집기가 개장 뒤에 시작했다). Markov가 그날 기본 전이행렬로 돈다. `runs`에 `no_regime`으로 남는다. 할 일은 없고, 매일 나오면 07:30 수집을 확인한다. |
| 디스크가 찬다 | `df -h`, `du -sh data/*/` | 로컬 데이터는 자동으로 지우지 않는다. 지우기 전에 `docker compose run --rm engine archive --recheck`를 실행하고, `0 failed`로 끝나는지(종료 코드 0) 확인한다. `.uploaded.json`이 있다는 것만으로는 지금 파일이 S3에 있다고 볼 수 없다. 파티션을 합친 뒤 업로드가 실패하면 그 전 파일의 기록이 남아 있기 때문이다. 확인한 뒤에는 오래된 날짜 폴더를 `raw`, `events`, `gaps`에서 지워도 된다. `bars`는 지우지 않는다(백필이 다시 받는다). `state/training-cache/`는 밤 학습이 끝날 때 그 밤에 쓰지 않은 오래된 날짜를 지운다. 남은 것도 지워도 된다(밤 학습이 S3에서 다시 받는다). |
| 서버를 잃었다 | Lightsail 콘솔의 스냅샷 | 1. 옛 인스턴스가 아직 있으면 먼저 멈추거나 지운다. 같은 Tailscale 신원으로 두 대가 동시에 뜨지 않게 하려는 것이다.<br>2. 마지막 자동 스냅샷으로 새 인스턴스를 만든다. 디스크에 Tailscale 신원, `.env`, 데이터가 그대로 있어서 부팅하면 `krx-quant`로 다시 붙고 엔진도 스스로 뜬다. Tailscale admin의 기기는 지우지 않는다. 지우면 접속할 길이 없어진다.<br>3. 새 인스턴스의 Lightsail 방화벽은 41641/udp만 열어 둔다(2.2의 10단계 명령).<br>4. `docker compose run --rm engine archive --recheck`로 S3와 맞춘다.<br>마지막 스냅샷 뒤에 녹화하고 아직 올리지 못한 데이터는 잃는다. |

> **바뀜** (2026-10-12, #73): 위 표의 CPU 예산 행은 "텔레그램 알림은 없다"였다 → 하루 한 번 알린다. 이유: 운영자가 예산 문제를 로그로만 알 수 있었다(#71).

> **바뀜** (2026-10-12, #116): 위 표의 `[신호]` 행은 "같은 종목·방향은 4분 안에 다시 오지 않는다"뿐이었다 → 엔진을 다시 시작해도 4분 안에 다시 오지 않고, 막힌 신호는 오지 않고 기록만 남는다. 이유: 매매하면 안 되는 때에도 신호가 나갔고, 재시작하면 같은 신호가 다시 나갈 수 있었다(#80).

> **바뀜** (2026-10-14, #PR): `[장애] 장중 데이터가 끊긴 피드: stale <TR> <키>`가 키마다 왔다 → 한 피드의 키가 모두 조용할 때만 `[장애] 구독한 키가 모두 끊긴 피드: stale <TR>`가 온다. 키 하나의 결측은 기록만 한다. 이유: 체결이 30분 넘게 없는 옵션 행사가처럼 시장 때문에 조용한 키마다 알림이 와서, 운영자가 고장일 때만 알림을 받기로 했다(#174).

## 7. 비용

- Lightsail 2GB: 월 $12. 데이터 전송 3TB가 포함된다.
- S3: 월 $1 미만으로 예상한다(#7에서 하루 데이터 크기를 잰다).
- 스냅샷: GB·월당 $0.05다.
- 상한은 월 $100이다. 밤 학습을 Spot으로 옮기면 서버의 Cost Guard가 AWS Budgets 예산(`krx-quant-monthly`)을 1시간마다 읽어, 50%·80%에서 알리고 100%에서 그달 Spot 학습을 끈다(3장 "밤 학습을 EC2 Spot으로 옮기기").
- Spot 학습: 하루 30분 안팎이면 월 $1 안팎으로 예상한다(미확인 ⚠). ECR: 월 $0.1 안팎.
- 지금은 AWS 프로젝트 지출 한도($100)가 걸려 있어서, 한도에 닿으면 상시 서버까지 멈춘다(1장 참고). Cost Guard보다 먼저 닿지 않게 한도를 올릴지는 사용자가 정한다.

> **바뀜** (2026-10-11, #67): "Budgets 알림과 Spot 학습 상한은 #23에서 만든다" → 서버의 Cost Guard가 한다. 계획은 AWS Budgets가 텔레그램으로 직접 알리는 것이었다. 이유: 텔레그램 토큰을 서버 `.env` 밖(Lambda, SSM)에 두지 않기 위해 서버가 예산을 읽어 알린다(사용자와 정함).

## 8. 저장 위치

엔진이 쓰는 파일과 Object Store 키 전체다. 형식은 링크한 문서에 있다.

**로컬 `data/`** (`KRX_DATA_DIR`, 컨테이너 `/data`). 녹화 형식은 [events.md](events.md)에 있다.

| 경로 | 내용 |
|---|---|
| `raw/date=<날짜>/feed=<TR>/symbol=<키>/` | 받은 메시지 원문. KIS 제어 메시지는 `feed=_system`, 알아볼 수 없는 메시지는 `feed=_unparsed` |
| `events/date=…/feed=…/symbol=…/` | 정규화한 이벤트 |
| `gaps/date=…/feed=…/symbol=…/` | 결측 구간 |
| 위 세 계층의 `part-<HHMMSS>-<id>.parquet`, `data.parquet` | 장중에 쓰는 part 파일과, 업로드가 합친 파일 하나 |
| `bars/date=…/feed=FHKST03010230/symbol=<종목>/bars.parquet` | 주식·ETF 1분봉 백필 |
| `bars/date=…/feed=FHKIF03020200/symbol=<KOSPI200F 또는 USDF>/bars.parquet` | 선물 연속 1분봉 백필 |
| `macro/date=…/feed=readings/symbol=<지표>/`, `macro/date=…/feed=regime/symbol=market/` | 매크로 지표와 그날 국면([events.md](events.md#매크로-지표와-일일-국면-8)). 다시 읽으면 줄이 더해지고, 마지막 줄이 최신이다. 업로드도 다른 계층과 같다 |
| 각 파티션의 `.uploaded.json` | 저장소별로 올린 파일의 크기, 수정 시각(ns), SHA-256. 바뀌지 않은 파일은 다시 올리지 않는다 |
| 각 파티션의 `.*.tmp` | 쓰는 중인 임시 파일. 10분 넘은 것은 업로드가 지운다 |
| `.archive.lock` | 업로드를 한 번에 하나만 돌리는 잠금 파일(`flock`) |

**로컬 `state/`** (`KRX_STATE_DIR`, 컨테이너 `/state`)

| 경로 | 내용 |
|---|---|
| `kis_token.json` | KIS REST 접근 토큰 캐시(권한 600). 앱키가 바뀌면 버린다 |
| `market_calendar.json` | KIS 휴장일 조회 캐시 |
| `engine.sqlite3`, `-wal`, `-shm` | 예측, 채점, 상태, 합의 가중치. 테이블은 [engine-api.md](engine-api.md#저장-stateenginesqlite3)에 있다. 스키마 버전은 `PRAGMA user_version`이다 |
| `replay-<날짜>.sqlite3` | `krx-quant replay`의 결과 |
| `prevalidation-<알고리즘>.sqlite3` | `krx-quant prevalidate`가 재생한 예측과 채점. 다시 돌리면 새로 만든다 |
| `prevalidations/<알고리즘>.json` | 사전 검증 리포트(`passed`, 검사별 결과, 통계). 엔진이 봉 마감마다 기다리는 알고리즘의 것을 읽는다([engine-api.md](engine-api.md#사전-검증-24)) |
| `models/<알고리즘>.json` | 엔진이 지금 돌리는 모델: `version`, `params`, `trained`, `trained_until`. 없으면 기본 모델 |
| `models/versions/<알고리즘>/<버전>.json` | 설치한 적 있는 모든 모델. `rollback`이 여기서 읽는다 |
| `models/history.json` | 설치와 되돌리기 이력. 항목마다 `at`, `algorithm`, `version`(기본 모델이면 `null`), `why`(`fit`, `promotion <날짜>`, `rollback from <버전>`, `installed before`) |
| `models/applied.json` | `{"night": "<날짜>"}`: 마지막으로 적용한 승격 목록의 밤. 이 밤과 그 전의 목록은 다시 적용하지 않는다 |
| `training-cache/` | 밤 학습이 Object Store에서 받아 둔 백필 1분봉과 녹화 이벤트의 사본. 키 경로가 같다. 학습이 끝나면 그 밤에 쓴 가장 오래된 날보다 오래된 날짜를 지운다 |
| `cost_guard.json` | Cost Guard가 이달 한 일(#23): `month`(UTC `YYYY-MM`), `warned`(알린 비율, %), `stopped`(Spot 스케줄을 끔), `stop_told`(끈 것을 알림), `spent`(이달 값으로 마지막에 읽은 비용), `carried`(지난달 마지막 비용. 이 값 이상이 보이는 동안은 지난달 값으로 본다). 재시작해도 다시 알리지 않으려고 둔다. 잃어도 다음 달 1일에 스케줄을 다시 켠다 |
| `spot_summarized.json` | `{"night": "<날짜>"}`: 요약을 보낸 마지막 Spot 학습 밤(#23). 재시작해도 같은 요약을 다시 보내지 않는다. 보내지 못한 요약은 적지 않아서, 기한까지 다시 시도하고 다음 기다림(재시작)에도 보낸다 |

> **바뀜** (2026-10-10, #48): 이전에는 `training-cache/`를 자동으로 지우지 않았다 → 지금은 밤마다 오래된 날짜를 지운다. 이유: 녹화 이벤트도 캐시하게 되어 크기가 커졌다.

**서버의 저장소 폴더** (`~/krx-quant`)

| 경로 | 내용 |
|---|---|
| `.env` | 설정과 비밀값(권한 600). 저장소에 커밋하지 않는다 |
| `.auto-update-deployed` | 엔진이 마지막으로 정상 기동을 확인한 커밋 |
| `.auto-update-failed` | 시작하지 못한 커밋. main이 바뀔 때까지 다시 시도하지 않는다 |
| `.auto-update-push-failed` | Spot 학습용 이미지를 ECR에 올리지 못했다(#23). 다음 자동 업데이트가 새 커밋이 없어도 다시 올리고 지운다 |

**Object Store** (`KRX_OBJECT_STORE`, 서버에서는 `s3://<버킷>/engine`)

| 키 | 내용 |
|---|---|
| `raw/…/data.parquet`, `events/…/data.parquet`, `gaps/…/data.parquet` | 로컬 `data/`와 같은 경로. 장 마감 15분 뒤와 21:00에 올린다 |
| `bars/…/bars.parquet` | 1분봉 백필. 로컬과 같은 경로 |
| `models/<알고리즘>/<버전>.json` | 밤 학습의 challenger와 서버가 돌린 모델. 형식은 `state/models/<알고리즘>.json`과 같다 |
| `reports/<날짜>/<알고리즘>.json` | 그 밤의 알고리즘별 검증 리포트이자 체크포인트: `algorithm`, `day`, `training_days`, `validation_days`, `champion`(`version`, `loss`, `trained_on_validation`: 검증일까지 학습한 모델인지. 손으로 `fit`한 모델 등), `challenger`(`version`, `loss`, `trained`), `promoted`, `reason`, `reference_prices`(학습·검증일마다 `mid`: 녹화 중간가, `close`: 백필 종가), `samples`(`validation`: 검증 표본 수, 검증까지 간 경우만), `validation`(검증까지 간 경우만, #89: `by_day`는 검증일마다, `by_reference`는 `mid`·`close`마다 `samples`·`challenger`·`champion` 평균 log loss. champion이 하나도 예측하지 않았으면 `champion`이 `null`. `paired`는 두 모델이 모두 예측한 표본 수 `samples`, 손실 차이(challenger − champion) 평균 `mean_difference`, 그 평균의 표준오차 `day_standard_error`(날마다 잔차를 더한 합들을 독립으로 본 클러스터 표준오차. 날의 표본 수가 같으면 검증일 평균들의 표준오차와 같다. 검증일이 2일보다 적으면 `null`)), `commit`(학습한 코드, 엔진처럼 `KRX_COMMIT`. 없으면 `unknown`, #85). 학습하지 못했거나 검증할 정답이 없으면 `champion`, `challenger`가 `null`이다 |
| `promotions/<날짜>.json` | 그 밤의 승격 목록: `day`, `finalized_at`(확정 시각), `promoted`(알고리즘 → 버전), `reports`(위 리포트 전부). 모든 알고리즘이 끝난 뒤 한 번 쓴다 |
| `live.json` | 서버가 지금 돌리는 버전(알고리즘 → 버전, 기본 모델이면 `<이름>-default`). 다음 밤의 champion이다. 사전 검증을 기다리는 알고리즘은 돌기 시작할 때 쓸 모델이다(#24) |
| `training/settings.json` | `KRX_TRAIN_ON=spot`일 때 서버가 올리는 학습 설정(#23): `KRX_SYMBOLS`, `KRX_ETF_SYMBOLS`, `KRX_US_SYMBOLS`, `KRX_INDEX_CODES`, `KRX_FEE_PERCENT`. 비밀값은 없다. 수집기가 시작할 때와 밤마다 올린다 |
| `training/market_calendar.json` | 같은 때 올리는 서버의 휴장일 달력(`state/market_calendar.json`과 같은 형식). Spot 학습이 옵션 만기를 서버와 같게 센다 |
| `training/logs/<KST 시각>.log` | Spot 인스턴스의 실행 로그. 마지막 줄이 `krx-quant training: exit <코드>`다 |

- 객체마다 내용의 SHA-256을 함께 둔다. S3에서는 객체 메타데이터 `sha256`이고, 로컬 폴더에서는 파일에서 계산한다. 업로드와 밤 학습은 이 값으로 같은 파일인지 본다.
- 엔진의 IAM 사용자는 객체를 지울 수 없다. 덮어쓴 이전 버전은 S3 버전 관리로 30일 남는다(2.1).
