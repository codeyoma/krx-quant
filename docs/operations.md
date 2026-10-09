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

   이 프로젝트에는 월 $100 지출 한도도 걸려 있다. 한도에 닿으면 프로젝트 전체가 일시정지되고 자원이 멈춘다. 상시 서버도 멈춘다는 뜻이다. 조기 통제인 "Stop new resource launches"도 켜져 있다. 예상 비용이 월 $15 정도라서 지금은 그대로 둔다. 설계는 상한에 닿아도 실시간 수집을 계속하도록 되어 있으므로 #23(비용 상한)에서 다시 정한다.
2. **KIS Open API**: 실전계좌로 앱키와 앱시크릿을 발급받는다. 모의투자 키로는 통합 시세 피드가 오지 않는다.
3. **텔레그램 봇**
   1. BotFather에게 `/newbot`을 보내 봇을 만들고 토큰을 받는다.
   2. 만든 봇에게 아무 메시지나 보낸다.
   3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates`를 열어 `chat.id`를 확인한다.
4. **Tailscale**
   1. Tailscale에 가입한다. 맥과 윈도우에 앱을 설치하고 같은 계정으로 로그인한다.
   2. (선택) 서버를 브라우저 승인 없이 붙이려면 admin console → Settings → Keys에서 auth key를 만든다. 1회용으로, 만료는 짧게 둔다.
5. **AWS 자원 만들기**: 2.1절의 스크립트를 실행한다.
6. **서버 개통**: 2.2절을 따라 한다.

## 2. 개통

### 2.1 AWS 자원 (맥에서)

버킷 이름은 전 세계에서 유일해야 한다(예: `krx-quant-<이름>`).

```bash
deploy/create-aws.sh krx-quant-<이름>
```

리전은 `AWS_REGION` 환경 변수, 없으면 `aws configure`에서 정한 리전(`ap-southeast-2`)을 쓴다. 모든 자원을 이 리전 하나에 만든다.

이 스크립트가 만드는 것이다. 다시 돌려도 이미 있는 자원은 그대로 둔다.
- S3 버킷: 시드니 리전에 만들고 공개 접근을 모두 막는다. 버전 관리를 켜서 덮어쓴 이전 버전을 30일 보관한다. 엔진은 지울 수 없지만 덮어쓸 수는 있어서, 잘못된 파일로 덮어써도 되돌릴 수 있게 하려는 것이다.
- IAM 사용자 `krx-quant-engine`: 이 버킷만 읽고 쓸 수 있다. 삭제 권한은 없다(`deploy/iam-policy.json`).
- Lightsail 인스턴스 `krx-quant`: Ubuntu 24.04다. 번들은 2GB, 공인 IPv4인 리눅스 번들을 자동으로 고른다(시드니는 `small_3_2`). 번들 ID가 리전마다 달라서다. `BUNDLE`로 직접 정할 수도 있다.
- Lightsail 방화벽: TCP 22는 Lightsail 브라우저 SSH에서만, UDP 41641(Tailscale 직접 연결)은 모두에게 연다. 나머지는 모두 닫는다.
- 자동 스냅샷: 매일 18:00 UTC(03:00 KST)에 뜬다. Lightsail은 최근 7개를 보관한다.

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
   - Docker가 Tailscale 뒤에 시작되도록 순서를 정한다.
   - 방화벽을 설정한다: 들어오는 연결은 Tailscale 인터페이스, 22번, 41641/udp만 허용한다.
   - 첫 실행에서는 `.env`를 `.env.example`에서 만들고(권한 600) 멈춘다.
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
   | `KRX_SYMBOLS`, `KIS_WS_MAX_SUBSCRIPTIONS` | 기본값 그대로(`005930,000660,009150`, `41`) |
   | `KRX_BIND_ADDRESS` | 비워 둔다. 스크립트가 Tailscale 주소를 넣는다 |
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
```

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

## 3. 매일 일어나는 일

| 시각 (KST) | 일 |
|---|---|
| 장중 | 실시간 녹화. 끊김과 지연은 `gaps`에 기록하고 텔레그램으로 알린다 |
| 09:01–15:16 매분 | 기준 예측을 내고, 4분 뒤 채점한다. `state/engine.sqlite3`에 쌓인다 |
| 장 마감 15분 뒤 (보통 15:45, 수능일 16:45) | 그날 녹화를 파티션마다 합쳐 S3에 올린다 |
| 20:30 | 휴장일 정보를 갱신하고 1분봉 백필을 돌린다 |
| 21:00 | 다시 올린다. NXT·애프터마켓의 늦은 데이터와 그날 1분봉이 들어간다 |
| 03:00 | Lightsail 자동 스냅샷 |

- 수집기가 시작할 때도 지난날 중 아직 안 올린 날을 올리고 백필을 돌린다.
- 올리는 작업은 다시 돌려도 결과가 같다. 바뀐 파일만 다시 올린다.
- 손으로 돌리려면 `docker compose run --rm engine archive`를 실행한다.
- 업로드 기록은 서버의 각 파티션 `.uploaded.json`에 저장소별로 남는다. S3 쪽에서 객체가 사라졌을 수 있으면 `docker compose run --rm engine archive --recheck`로 모든 파일을 S3와 비교해 빠진 것을 올린다.

## 4. 업데이트 배포

```bash
cd ~/krx-quant
git pull
docker compose up -d --build
```

- 엔진을 다시 띄우는 동안은 녹화가 빈다. 장중 배포는 피한다.
- 장 마감 후, 21:00 업로드가 끝난 뒤가 좋다.

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
| `[장애] 장중 데이터가 끊긴 피드: stale <TR> <키>` | 한 종목만이면 거래정지나 VI다. 여러 피드면 연결 문제다. | 데이터가 다시 오면 `[복구]`가 온다. 거래일에 한 번도 안 오는 구독이면 구독 키가 잘못됐을 수 있다(KIS는 잘못된 키도 구독 성공으로 답한다). |
| `[장애] 장 마감 데이터 업로드가 ... 실패했습니다` | 로그의 `could not archive ...` | 1. AWS 키, `KRX_OBJECT_STORE` 버킷 이름, IAM 정책을 확인한다.<br>2. 고친 뒤 `docker compose run --rm engine archive`를 돌린다. 다시 돌려도 안전하다.<br>3. 다음 업로드가 성공하면 `[복구]`가 온다. |
| `[장애] 엔진 API를 열지 못했습니다` | 로그의 `Engine API not started on ...` | 녹화와 예측은 계속한다. 다른 프로세스가 8080을 쓰는지 확인하고 `docker compose up -d`로 다시 띄운다. |
| 알고리즘을 다시 학습하고 싶다 | | 알고리즘마다(`newton`, `gauss`) `docker compose run --rm --no-deps engine fit <이름>` 뒤 `docker compose restart engine`. 모델은 `state/models/<이름>.json`이다. 되돌리려면 그 파일을 지우고 재시작한다(기본 모델). |
| `[알림] <이름> 은퇴 후보: ...` | `curl -s http://krx-quant:8080/v1/algorithms`의 `status` 이력, `/v1/algorithms/<이름>/stats?period=20d` | 벤치 60거래일 동안 회복하지 못했다. 은퇴시키려면 `docker compose exec engine krx-quant retire <이름>`. 기록은 남고, 엔진은 다음 봉 마감부터 돌리지 않는다. 되돌리려면 `krx-quant unretire <이름>`(수습부터 다시). |
| `[warning] ... skipped for the CPU budget` 또는 `took over 30s` 로그가 잦다 | `docker stats`로 CPU 사용률 | 알고리즘 계산이 서버에 버겁다. 4GB 요금제로 올리거나 `KRX_CPU_BUDGET_SECONDS`를 조정한다. |
| 녹화된 날의 예측을 다시 보고 싶다 | | `docker compose run --rm --no-deps engine replay 2026-10-12`. 결과는 `state/replay-2026-10-12.sqlite3`다. 실시간 DB는 건드리지 않는다. |
| 엔진 컨테이너가 계속 재시작한다 | 로그의 `configuration error` 또는 `KIS error` | `.env` 값을 고치고 `docker compose up -d`를 실행한다. |
| `EGW00133`(접근 토큰 발급 1분 1회) | 재시작을 반복했는지 | 토큰은 캐시되고, 1분 간격으로 스스로 다시 시도한다. |
| `EGW00201`(초당 거래건수 초과) | 백필과 REST 대체 조회가 겹쳤는지 | 0.5초 뒤 스스로 다시 보낸다. 계속되면 `KRX_BACKFILL_MAX_PER_SECOND`를 낮춘다. |
| 디스크가 찬다 | `df -h`, `du -sh data/*/` | 로컬 데이터는 자동으로 지우지 않는다. `.uploaded.json`이 있는 오래된 날짜 폴더는 S3에 있으므로 `raw`, `events`, `gaps`에서 지워도 된다. `bars`는 지우지 않는다(백필이 다시 받는다). |
| 서버를 잃었다 | Lightsail 콘솔의 스냅샷 | 1. 옛 인스턴스가 아직 있으면 먼저 멈추거나 지운다. 같은 Tailscale 신원으로 두 대가 동시에 뜨지 않게 하려는 것이다.<br>2. 마지막 자동 스냅샷으로 새 인스턴스를 만든다. 디스크에 Tailscale 신원, `.env`, 데이터가 그대로 있어서 부팅하면 `krx-quant`로 다시 붙고 엔진도 스스로 뜬다. Tailscale admin의 기기는 지우지 않는다. 지우면 접속할 길이 없어진다.<br>3. 새 인스턴스의 Lightsail 방화벽은 41641/udp만 열어 둔다(2.2의 10단계 명령).<br>4. `docker compose run --rm engine archive --recheck`로 S3와 맞춘다.<br>마지막 스냅샷 뒤에 녹화하고 아직 올리지 못한 데이터는 잃는다. |

## 7. 비용

- Lightsail 2GB: 월 $12. 데이터 전송 3TB가 포함된다.
- S3: 월 $1 미만으로 예상한다(#7에서 하루 데이터 크기를 잰다).
- 스냅샷: GB·월당 $0.05다.
- 상한은 월 $100이다. Budgets 알림과 Spot 학습 상한은 #23에서 만든다.
- 지금은 AWS 프로젝트 지출 한도($100)가 걸려 있어서, 한도에 닿으면 상시 서버까지 멈춘다(1장 참고).
