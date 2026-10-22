# KRX Open API 레퍼런스 (#305)

단일종목 ±2배 ETF 16종(`src/krx_quant/products.py`의 `PRODUCTS`)의 지난날 순자산과 상장좌수를 받을 출처인 한국거래소의 KRX Open API(https://openapi.krx.co.kr)를 정리한다. KIS는 순자산 총액과 상장 주수를 오늘 값만 준다(2026-10-21 `probe-nav-305`, [kis-feeds.md](kis-feeds.md) "ETF 순자산 (#305)"). 그래서 사용자가 2026-10-22에 지난날 순자산을 이 서비스의 "ETF 일별매매정보"(`etf_bydd_trd`)에서 받기로 정했다(#305 댓글).

**아래는 모두 서버에서 재지 않았다 ⚠.** 2026-10-22에 서비스 페이지와 FAQ를 읽어 적었다. 사용자가 인증키를 서버 `.env`에 넣으면(`KRX_OPENAPI_KEY`, [operations.md](operations.md) 1장 10번) 예약 작업 `probe-krx-etf-305`가 그 밤에 `krx-quant probe-krx-etf`로 재 #305에 남긴다([operations.md](operations.md#krx-open-api-etf-일별매매정보-확인-305)). 그 결과로 이 문서의 ⚠를 고친다. 그 전에는 엔진이 이 서비스의 값을 받지도 쓰지도 않는다(`CODING_STANDARDS.md` "External data is probed before it is used").

## 출처

- 서비스 페이지 [ETF 일별매매정보](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES003_S2.cmd?BO_ID=nrEpCLaZpoLCTzPUMxuF)(등록 2020-09-22, 최근 수정 2026-01-16). 출력값 표는 페이지의 스크립트가 그린다. 그 스크립트에 든 서비스 정의(입력·출력 블록과 조회 SQL을 담은 XML)를 읽었다. 내려받는 "개발 명세서"는 받지 않았다.
- [FAQ](https://openapi.krx.co.kr/contents/OPP/COMM/faq/OPPCOMM004.cmd)와 [이용약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp). 약관은 #305의 출처 조사 댓글(2026-10-22)에 정리했다.

## 조회 ⚠

- `GET https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd.json?basDd=YYYYMMDD`. 주소 끝의 `.json`이 JSON 응답이다(FAQ). 서비스 페이지의 샘플 경로는 `/svc/sample/apis/etp/etf_bydd_trd`이고, 정식 경로는 `/svc/apis/…`다(FAQ의 403 항목). 엔진은 `KRX_OPENAPI_URL`(기본 `https://data-dbg.krx.co.kr`)에 이 경로를 붙인다.
- 인증키는 요청 헤더 `AUTH_KEY`에 넣는다(서비스 페이지). 주소에는 넣지 않는다.
- 입력은 `basDd`(기준일자, 8자) 하나다. 한 번에 한 날만 묻는다(FAQ).
- 한 번 부르면 그날 장 종료 때 상장돼 있던 모든 ETF(유가증권·코스닥, 조회 SQL의 증권 그룹 `EF`·`FE`)가 온다. 뒤에 상장폐지된 종목도 그날의 답에는 든다(FAQ).
- 2010-01-04부터 준다(서비스 페이지).

## 출력 ⚠

블록 `OutBlock_1`의 행 목록이다. 서비스 정의에서 모든 필드는 string이고, 숫자 필드는 값이 없으면 `-`다(`default="-"`). 숫자에 천 단위 쉼표가 붙는지는 정의로는 모른다 ⚠.

| 필드 | 이름 | 형식(서비스 정의) | 메모 |
|---|---|---|---|
| `BAS_DD` | 기준일자 | | 물은 `basDd` 그대로다(조회 SQL) |
| `ISU_CD` | 종목코드 | | KRX 단축코드(`ISU_SRT_CD`)에서 앞 한 글자를 뺀 것이다(SQL의 `SUBSTR(ISU_SRT_CD, 2)`). 그래서 `0193W0`처럼 6자리일 것이다 ⚠ |
| `ISU_NM` | 종목명 | | |
| `TDD_CLSPRC` | 종가 | `###0` | 원, 정규시장 가격(FAQ) |
| `CMPPREVDD_PRC`, `FLUC_RT` | 대비, 등락률 | `###0`, `###0.00` | |
| `NAV` | 순자산가치(NAV) | `###0.00` | |
| `TDD_OPNPRC`, `TDD_HGPRC`, `TDD_LWPRC` | 시가, 고가, 저가 | `###0` | 정규시장 체결이 없으면 0(FAQ) |
| `ACC_TRDVOL`, `ACC_TRDVAL` | 거래량, 거래대금 | `###0` | 주, 원. 시간외와 애프터마켓(2026-09-14부터)도 든다(FAQ) |
| `MKTCAP` | 시가총액 | `###0` | |
| `INVSTASST_NETASST_TOTAMT` | 순자산총액 | `###0` | 그날의 종목별 매매 표에서 온다(SQL의 `TBMD_BYDD_ISU_TRDPRC`). 단위(원인지)와 그날 마감의 값인지 ⚠ |
| `LIST_SHRS` | 상장좌수 | `###0` | 종목 마스터(SQL의 `TBMC_SPOT_ISU`)에서 그날이 유효 기간(`STRT_DD`–`END_DD`) 안인 행의 값이다. 설정·환매로 날마다 바뀐 좌수인지 ⚠. `MKTCAP ÷ TDD_CLSPRC`와 견줄 수 있다 |
| `IDX_IND_NM` | 기초지수명 | | |
| `OBJ_STKPRC_IDX`, `CMPPREVDD_IDX`, `FLUC_RT_IDX` | 기초지수 종가, 대비, 등락률 | `###0.00` | 지수를 공표하지 않는 상품은 비어 있다(SQL) |

- 가격은 그날의 원래 가격이다. 액면분할 등으로 지난 값을 고치지 않는다(FAQ "수정주가"). 확정된 값만 주고 정정 이력은 주지 않는다(FAQ).
- 16종의 새 코드(글자가 든 6자리)가 답에 드는지 ⚠. 조회가 코드 모양으로 거르지 않으니 들 것으로 본다.

## 언제 나오나 ⚠

- 영업일의 값은 다음 영업일 08:00부터 준다. 금요일 것은 월요일 08:00이다. 그날 값과 장중 값은 주지 않는다. 주말·공휴일은 답이 없다(FAQ).
- 조회 SQL도 비슷하다: 기준일이 "어제까지의 가장 최근 개장일"보다 앞이면 주고, 그날이면 08시부터 준다. 금요일 값을 토요일 08:00에 주는지(SQL로 읽은 것) 월요일에 주는지(FAQ)는 재야 안다 ⚠.
- 그래서 그날의 A(전 거래일 순자산)는 그날 08:00부터 받을 수 있다. 밤에 물으면 그날 것은 없고 그 전 거래일 것까지다.

## 한도와 오류 ⚠

- 인증키당 하루 10,000번이고 자정에 초기화된다. 초당·분당 한도는 없다(FAQ). 탐사는 1초 간격으로 부른다.
- 401: 인증키만 받고 이 서비스를 이용 신청·승인받지 않음, 갱신 전 인증키, 오타·공백. 403: 주소·인증키의 오타, 샘플 경로, http. 429: 하루 한도를 넘음. 302: http로 불러 https로 넘어감(FAQ). 오류 본문의 모양은 모른다 ⚠.
- pykrx 같은 비공식 라이브러리로 data.krx.co.kr를 긁으면 IP를 막는다(FAQ). 이 시스템은 Open API만 부른다.

## 인증키와 약관

- KRX Data Marketplace 계정 → openapi.krx.co.kr 마이페이지에서 API 인증키 신청 → 서비스 이용 → 증권상품 → ETF 일별매매정보 → API 이용신청(12개월, 신청 목적 "개인 연구"). 인증키와 서비스 모두 관리자 승인을 기다린다(영업일 09:00에 전날 접수분부터). 인증키만으로는 부를 수 없다(401).
- 이용 기간은 저절로 갱신되지 않는다. 만료 30일 전부터 마이페이지 → 이용현황 → API 이용현황에서 다시 신청한다. 갱신하면 이전 인증키는 쓸 수 없다(FAQ).
- 서버 `.env`의 `KRX_OPENAPI_KEY`에 둔다(`.env.example`). 저장소, 이슈, 로그에는 두지 않는다. 예약 작업의 댓글과 `krx-quant doctor`에서 가린다(`redaction.secrets_of`).
- 약관: 비상업 목적(개인 연구·투자)만, 제3자 제공·재배포 금지, 이용 기간이 끝나면 받은 데이터를 쓰지 않음, 화면에 "한국거래소 통계정보" 표시(#305 조사 댓글). 그래서 받은 값은 서버 밖으로 내보내지 않는다(토스 값과 같은 규칙, [design.md](design.md) 4.7). 탐사도 시세 값을 내지 않는다.

## 탐사가 재는 것 (`krx-quant probe-krx-etf`, `src/krx_quant/krx_etf_probe.py`)

상장 전 거래일(2026-05-26), 상장일(2026-05-27), 그 뒤 주마다 첫 거래일, 마지막 5거래일을 묻는다(30번쯤). 답에 오는 필드를 모두 읽고, 위 표의 어느 것도 있다고 가정하지 않는다. 시세 값은 내지 않는다(개수, 날짜, 필드 이름과 모양, 같음·다름만).

1. 날마다의 답: 행이 있는 날, 빈 답, 실패(401·403·429는 FAQ의 뜻과 함께), 본문 키, 그날의 행 수, 상품 행의 `BAS_DD`가 물은 날인지.
2. 필드: 위 표의 19개와 견준 있음·없음·더 있음, 모양(`int`, `dec`, `text`, `empty` 등), 천 단위 쉼표와 `-`가 있는 필드, 순자산총액·상장좌수·NAV·시가총액·종가의 값이 있는 상품 행 수.
3. 종목코드: 상품 코드가 든 필드와 쓰는 법(6자리 그대로, `A` 붙음, 12자리 표준코드 안), 찾지 못한 상품.
4. 상장일부터 행이 있나: 상품마다 상장일과 그 뒤 물은 거래일 중 행이 있는 날, 없는 날. 상장 전 거래일의 상품 행.
5. 그날 값이 언제 나오나: 물은 때 마지막 거래일과 그 전 거래일의 답.
6. `LIST_SHRS`가 날마다 바뀌나: 이웃한 거래일 짝과 더 떨어진 짝에서 바뀐 수, 바뀐 상품. `MKTCAP ÷ TDD_CLSPRC = LIST_SHRS`인 행 수.
7. `INVSTASST_NETASST_TOTAMT = LIST_SHRS × NAV`가 어느 단위(원, 천원, 백만원, 억원)에서 맞는지.
8. KIS와 같은 날: 물은 날마다 KRX `NAV`와 KIS `FHPST02440200`(NAV 비교추이(일))의 `nav`. 그 전 거래일과 마지막 거래일의 KRX 순자산총액·상장좌수·NAV와 그때 물은 KIS `FHPST02400000`(ETF/ETN 현재가)의 `etf_ntas_ttam`·`lstn_stcn`·`prdy_last_nav`·`nav`.
