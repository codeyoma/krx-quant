# 토스증권 Open API 레퍼런스

일별 수급 지표(#226)의 보조 출처이자 외국인 보유수량·지분율의 주 출처, 그리고 화면 종목 패널의 회사 이름(#250)의 주 출처인 토스증권 Open API를 정리한다. 2026-10-15에 공식 문서(스펙 `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` 1.2.24, [소개](https://openapi.tossinvest.com/openapi-docs/overview.md), [FAQ](https://openapi.tossinvest.com/openapi-docs/faq.md))를 읽어 적었다. 서버에서는 아직 부르지 않았다. 문서에 없는 것은 ⚠다. 예약 작업 `probe-supply-226`이 `krx-quant probe-supply`로 재고 결과를 #226에 남긴다([operations.md](operations.md#일별-수급-출처-확인-226)). 그 결과로 ⚠를 지운다.

시세 데이터만 읽는다. 같은 클라이언트로 주문도 낼 수 있지만, 주문·조건주문 엔드포인트는 저장소에 둘 수 없다(`scripts/check_forbidden.py`가 막는다, #235).

## 출처 결정 (#226, 2026-10-15)

- KRX OPEN API, KRX 정보데이터시스템, 공공데이터포털, KOFIA FreeSIS로는 8종을 받을 수 없다(#226 댓글). **기관·개인 보유수량은 어느 출처에도 없어, 지표를 `일별 외국인 보유수량`으로 줄였다.**
- **KIS가 주 출처, 토스가 보조다.** 외국인 보유수량·지분율만 토스가 주 출처다(KIS는 그날 한 값과 30거래일 소진율뿐이다, [kis-feeds.md](kis-feeds.md#일별-수급-226)).

| 지표 | 주 출처 | 보조 |
|---|---|---|
| 공매도 거래량 | KIS `FHPST04830000` | 토스 `short-selling` |
| 개인·기관·외국인 순매수 | KIS `FHPTJ04160001` | 토스 `investor-trading` |
| 외국인 보유수량·지분율 | 토스 `investor-trading`의 `foreignerHolding` | KIS `FHKST01010400`(30거래일 소진율), `FHKST01010100`(그날 값) |
| 대차잔고 | KIS `HHPST074500C0` | 토스 `securities-lending` |
| 신용잔고 | KIS `FHPST04760000` | 토스 `credit-trades` |

## 인증과 접속

- 주소: `https://openapi.tossinvest.com`(`TOSS_URL`). 모든 호출에 `Authorization: Bearer <토큰>`을 붙인다. 시세·종목 정보는 계좌 헤더가 필요 없다.
- 토큰: `POST /oauth2/token`, `application/x-www-form-urlencoded` 본문 `grant_type=client_credentials`, `client_id`, `client_secret`. 답은 OAuth2 형식(`access_token`, `token_type` `Bearer`, `expires_in` 초, 예시 86400)이다. refresh token은 없다. 만료되면 같은 엔드포인트로 다시 받는다. 발급은 `AUTH` 그룹(초당 5회)이다.
- **클라이언트당 유효한 토큰은 하나다.** 새로 받으면 이전 토큰은 바로 무효가 되어 401 `token-revoked`로 거부된다. 프로세스마다 따로 받으면 서로를 무효로 만든다. 그래서 이 시스템은 토큰 하나를 `state/toss_token.json`(권한 600)에 두고 모든 프로세스가 같이 쓴다(`src/krx_quant/toss.py`의 `TossTokens`, KIS의 `state/kis_token.json`과 같은 방식).
  - 캐시된 토큰이 없거나 만료됐을 때만(만료 1분 전부터는 만료로 본다) 새로 받는다. 받는 동안 파일 잠금(`state/.toss_token.lock`)을 잡아 두 프로세스가 같이 받지 않게 한다.
  - 401 `token-revoked`·`expired-token`·`invalid-token`을 받으면 캐시를 다시 읽는다. 다른 프로세스가 새 토큰을 넣어 뒀으면 그것을 쓰고, 아니면 한 번 새로 받는다. 한 클라이언트(`TossClient`)는 다시 받기를 한 번까지만 한다.
  - 토큰은 로그와 오류 메시지에 남기지 않는다.
- **허용 IP.** 토스증권 WTS 설정 > Open API > 허용 IP 관리에 등록한 IP에서만 부를 수 있다. 다른 IP에서는 토큰 발급이 403 `access_denied`(`IP address not allowed`), 다른 엔드포인트가 403 `ip-not-allowed`다. `probe-supply`는 이것을 "the server's public IP is not in Toss's allowed IPs (WTS 설정 > Open API > 허용 IP 관리)"로 보이고 토스 조회를 멈춘다. 공인 IP가 바뀌는 환경이면 막힌다(FAQ). 서버(Lightsail)의 고정 IP를 등록한다.
- 오류 본문: 시세 엔드포인트는 `{"error": {"requestId", "code", "message", "data"}}`, 토큰 엔드포인트는 OAuth2 형식 `{"error", "error_description"}`이다. 429는 `Retry-After`만큼 기다린다(`toss.py`는 5초까지, 3번).
- **데이터 이용 정책(FAQ).** 받은 정보는 본인의 매매 목적에만 쓴다. 비상업적이어도 제3자에게 배포하면 안 된다. 그래서 공개 이슈·저장소에는 값을 올리지 않는다(`probe-supply`는 필드 이름, 개수, 날짜, 표시만 보인다).

## 종목 정보 (#250)

화면 종목 패널의 회사 이름이다(엔진 API `GET /v1/symbols`의 `name`, [engine-api.md](engine-api.md#get-v1symbols)).

- `GET /api/v1/stocks?symbols=005930,000660`: 콤마로 최대 200종목. `STOCK` 그룹. 답은 `{"result": [{"symbol", "name", "englishName", "isinCode", "market", "securityType", "status", …}]}`이다. 문서 예시의 `name`은 `삼성전자`, `KODEX 200`이다.
- 수집기가 시작할 때 한 번, 예측하는 종목을 모두 한 번에 묻는다(`src/krx_quant/names.py`). 토큰은 위의 공유 캐시를 쓴다. 클라이언트가 없거나, 허용 IP가 아니거나, 이름을 주지 않은 종목은 KIS 종목 마스터(`kospi_code.mst`, `kosdaq_code.mst`)의 한글 약칭을 쓴다. 2026-10-16에 005930, 000660, 009150, 133690, 381180의 두 이름이 같았다.
- 로고는 Open API에 없다. tossinvest.com의 종목 화면이 쓰는 `https://static.toss.im/png-icons/securities/icn-sec-fill-<종목코드>.png`(480×480 PNG, 인증 없음, 없는 코드는 403)를 엔진이 주소로만 주고 화면이 받는다. 2026-10-16에 tossinvest.com의 삼성전자 화면에서 확인했다(사이트는 `images.tossinvest.com`으로 줄여 보이고, 주소 뒤에 `?20240508` 같은 갱신 표시가 붙은 종목도 있다).
- 이름과 로고는 저장소와 디스크에 두지 않는다(아래 데이터 이용 정책). 화면은 실행 중 메모리에만 둔다.

## 일별 수급 엔드포인트

`GET /api/v1/stocks/{symbol}/<이름>`. 국내 종목만 된다(다른 시장은 400 `unsupported-market`, 없는 종목은 404 `stock-not-found`). 모두 `STOCK_TRADING_TREND` 그룹으로 초당 10회다. `probe-supply`는 초당 5회(0.2초 간격)로 부른다.

- 파라미터: `count`(1–100, 기본 10), `until`(`YYYY-MM-DD`, 그날 포함, 없으면 최신부터).
- 답: `{"result": {"nextUntil", "records": [...]}}`. 기록은 최신순이다. 다음 쪽은 `nextUntil`을 `until`로 넘긴다. 더 없으면 `nextUntil`이 null이다. 문서 예시에서 `nextUntil`은 마지막 기록의 전날이다.
- 수량은 모두 주 단위 정수 문자열, 금액은 원 단위 정수 문자열, 비율은 소수 비율 문자열(`0.5089` = 50.89%)이다.
- 거슬러 가는 끝은 문서에 없다 ⚠. `probe-supply`가 쪽을 넘겨 잰다(첫 종목, 30번까지 = 약 12년).

| 이름 | 주는 것 | 기록 필드 | 반영 시각(문서) |
|---|---|---|---|
| `investor-trading` | 투자자별 매매동향(KRX+NXT 통합 거래량) | `date`, `updatedAt`, `individual`, `foreigner`(등록외국인), `institution`(`breakdown`: 금융투자·보험·투신·사모·은행·기타금융·연기금), `otherCorporation`, 각각 `buyVolume`·`sellVolume`·`netBuyVolume`. `foreignerHolding`(`holdingQuantity` 보유 주식 수, `limitQuantity` 한도 주식 수, `holdingRate` 상장주식수 대비 보유 비율), `cfd`(`buyBalanceQuantity`·`buyBalanceRate`·`sellBalanceQuantity`·`sellBalanceRate`) | 매매동향 확정치와 외국인 보유는 그날 저녁. 외국인 보유는 다음 영업일 오전에 확정치로 한 번 더 바뀔 수 있다. CFD는 다음 영업일 새벽(T+1) |
| `short-selling` | 공매도 | `date`, `updatedAt`, `shortSellingVolume`(주), `shortSellingAmount`(원), `shortSellingVolumeRate`(소수 다섯째 자리), `shortSellingAmountRate`(소수 넷째 자리). 비율의 분모는 시간외·애프터마켓을 포함한 그날 누적이고, 분모가 없는 날은 null | 그날 저녁 |
| `credit-trades` | 신용거래 | `date`, `updatedAt`, `marginLoan`(신용융자), `stockLoan`(신용대주), 각각 `newQuantity`·`returnQuantity`·`balanceQuantity`·`balanceRate`(상장주식수 대비)·`tradingRate`(공여율). 자기신용과 유통금융의 합. 한쪽 데이터만 있는 날은 다른 쪽이 null | 다음 영업일 새벽(T+1). 최신 기록은 전 영업일 |
| `securities-lending` | 대차거래(기관 간) | `date`, `updatedAt`, `executionQuantity`, `repaymentQuantity`, `balanceQuantity`(대차 잔고 주수), `balanceAmount`(원) | 그날 저녁 |
| `program-trades` | 프로그램매매(8종 밖) | `date`, `arbitrage`, `nonArbitrage`, 각각 매수·매도·순매수 거래량. KRX만 집계하고 NXT는 넣지 않는다 | 당일 기록도 나오고 장 마감까지 바뀐다 |

**당일 기록은 잠정치다.** `investor-trading`의 당일 기록에서 `individual`, `institution.breakdown`, `otherCorporation`, `foreignerHolding`, `cfd`는 null이다. 저녁에 확정치가 들어온다. 그래서 수집은 그날 밤이나 다음 날 읽는다. 몇 시에 다 들어오는지는 ⚠(기록의 `updatedAt`으로 본다).

**KIS와 다른 점.**
- 토스의 `foreigner`는 등록외국인만이다. KIS `FHPTJ04160001`의 `frgn_ntby_qty`는 등록외국인과 기타외국인의 합이고, `frgn_reg_ntby_qty`가 등록외국인이다. `probe-supply`가 두 쪽 모두와 견준다.
- 토스의 `holdingRate`는 상장주식수 대비(지분율)다. KIS의 `hts_frgn_ehrt`는 외국인 한도 대비(소진율)다. 한도가 상장주식의 100%인 종목에서만 같다. `holdingQuantity ÷ limitQuantity`가 소진율이다.
- 토스의 신용 `date`는 매매일이다. KIS `FHPST04760000`은 결제일(`stlm_date`)로 묻고 매매일(`deal_date`)도 준다. `probe-supply`가 두 날짜 기준 모두로 견준다.

## 확인할 것 ⚠

`probe-supply`의 토스 절(#226 댓글)로 확인하고 이 문서를 고친다.
- 엔드포인트마다 거슬러 가는 끝과 빈 기간.
- `individual`, `foreignerHolding`, `cfd`, `institution.breakdown`, 공매도 비율, `stockLoan`이 어느 날부터 null이 아닌지.
- KIS와 같은 값을 주는지(순매수: 통합 `UN`인지 KRX `J`인지, 외국인: 등록외국인인지, 보유율·소진율, 공매도, 대차, 신용: 매매일인지 결제일인지).
- KIS `FHKST01010100`의 그날 보유수량이 토스의 어느 날 값과 같은지(그날인지 전 영업일인지).
