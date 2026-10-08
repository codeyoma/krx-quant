# 유료 데이터 조사

- 조사일: 2026-10-08
- 방침: 무료 우선. 유료는 필요가 입증되면 구매한다.
- ⚠ = 확인되지 않은 항목

## 결론

v1에서 꼭 사야 할 유료 데이터는 없다. 유료라고 예상했던 데이터 상당수를 KIS가 무료로 제공한다. 미국 주간거래 실시간, 프로그램매매·회원사 흐름, 옵션 IV가 그렇다.

## 조사표

| 항목 | 4분 예측에 쓸모 | 무료 출처 | 유료 업체 | 월 비용 | 제공 방식 / 지연 | 개인 가입 | 비고 |
|---|---|---|---|---|---|---|---|
| A. 미국 지수선물 NQ/ES/MNQ (CME Globex) | KRX 장중에 실시간으로 보이는 미국 위험선호. 반도체 대형주와 같이 움직인다. | 없음. KIS 해외선물은 CME 유료시세를 신청해야 종목을 등록할 수 있다[1]. KRX 상장 나스닥·필라델피아반도체 ETF를 간접 대용으로 쓸 수 있다. | KIS CME 유료시세, IBKR, Databento, AMP/CQG | CME 원가(비전문가): 1호가 $1.55/거래소, 4거래소 번들 $4.65, 호가잔량 $12.10[2]. KIS 청구액 ⚠. IBKR 번들 $10[3]. Databento Standard $199[4]. AMP 1호가 $5[5] | KIS WebSocket `HDFFF020`/`010`, 실시간 | 가능 (비전문가 자가인증). IBKR은 PRO 계좌와 잔고 $500 필요[6] | 자동·반자동 매매 시스템이 쓰면 Non-Display 라이선스(A8 $457/월)가 필요할 수 있다[2][7]. 예측 엔진이 해당하는지 ⚠ |
| B. 미국 개별주 장중 실시간 (NVDA/MU/AMD) | KRX 장중이 미국 주간거래 시간과 겹친다. 하이닉스와 MU·NVDA 사이에 선후행이 있다. | **KIS `HDFSCNT0` 주간거래 실시간 무료**[8]. 2025-11-04 재개, Blue Ocean·Bruce·Moon ATS 경유[9] | Blue Ocean 배포처(ICE, QUODD, NYSE Cloud, Pyth)[10][11], Massive Advanced[12] | 기관 견적, Massive $199 | 실시간 | KIS로 충분 | 미국 서머타임 해제 기간에는 10:00 KST부터라 개장 첫 1시간이 빈다. Nasdaq/SIP 23×5 체제는 2026-12-06 목표[13] |
| C1. 일본 반도체 (도쿄일렉트론 8035, 어드반테스트 6857) | 도쿄 거래 시간이 KRX와 거의 같다. 장비 업종이 동조한다. | KIS 15분 지연[8] | KIS 실시간 (HTS [7781]에서 신청), IBKR | KIS 금액 ⚠ (참고: 메리츠 일본 비전문가 ¥160[14]) | WebSocket 실시간 | 가능 | 닛케이 선물은 OSE 비전문가 $1 ⚠[15] |
| C2. 대만 TSMC 2330, TAIEX 선물 | 파운드리·메모리 업황 동조 | 없음 | TWSE 계약 벤더[16], TAIFEX는 Phillip Nova 경유 | TAIFEX 비전문가 €4 (면제 공지와 상충 ⚠)[17] | 벤더별 | 해외 개인은 어렵다 ⚠ | Fugle은 대만 계좌 필요 ⚠ |
| C3. 항셍/HSTECH 선물, SGX A50·닛케이 | 아시아 위험선호, 외국인 수급 | KIS 홍콩 주식 15분 지연[8] | KIS SGX 유료시세[1], IBKR | IBKR: HKFE 1호가 HKD 25, SGX 파생 SGD 1[3] | 실시간 | 가능 | KIS HKEX·OSE 선물이 무료 실시간인지 ⚠ |
| D. 원·달러 현물(서울외국환중개), NDF | 외국인 수급과 역상관 | KIS KRX 미국달러선물 (사용 중). 서울중개 앱 실시간 호가는 테스트 중이며 유료화 예정[19] | 연합인포맥스, 코스콤 CHECK Expert+, Bloomberg, LSEG | Bloomberg 약 $2,300 (업체 추정[21]). 인포맥스·CHECK 비공개 ⚠ | 단말기. API는 별도 계약 | 기관 위주 ⚠ | 2026-07-06부터 원·달러 중개 24시간[20]. 달러선물이 현물을 거의 따라가서 추가 가치가 낮다. |
| E. KRX 과거 틱·호가(L2) | 호가 피처 학습용. 1분봉 1년으로는 부족하다. | **KIS WebSocket으로 직접 녹화** (`H0STCNT0`/`H0STASP0`/`H0UNASP0`)[22][29] | KRX 정보데이터시스템 호가장·체결장[23], Tick Data LLC[25], LSEG Tick History[26], ICE[27]. Databento는 KRX 미지원[4] | 모두 비공개 ⚠. KRX 과거 데이터 30–40% 인상 보도[24] | 파일 / 클라우드 | KRX 개인 구매 가능 여부 ⚠ | NXT 데이터는 2027-03부터 유료화 (KRX 요율의 절반 예상)[24] |
| F. 실시간 뉴스·공시 | 공시·속보 직후 수 분 안에 가격이 반응한다. | DART OpenAPI(조회), KIS 뉴스제목 `FHKST01011800`[22] | 연합인포맥스, 이데일리 마켓포인트, 뉴스핌, Bloomberg | 개인 가격 비공개 ⚠ | 단말기 / 피드 | ⚠ | 무료 경로로 우선 충분하다. |
| G. 장중 투자자·프로그램·회원사 | 프로그램매매와 외국계 창구 흐름이 단기 방향을 보여준다. | **KIS 실시간 `H0STPGM0`/`H0UNPGM0`(종목 프로그램), `H0UPPGM0`(지수 프로그램), `H0STMBC0`/`H0UNMBC0`(회원사)**[22][29]. 추정 가집계 `HHPTJ04160200` (외국인 09:30·11:20·13:20·14:30)[22], 외국계 가집계 `FHKST644100C0` | 불필요 | – | 실시간 / 하루 몇 회 | – | 가집계는 증권사 직원이 입력한 추정치다. |
| H. KOSPI200 옵션 IV, V-KOSPI | 변동성 국면, 스큐 급변 | **KIS `H0IOCNT0`에 IV와 그릭스 포함**[30]. VKOSPI 지수 코드 ⚠ (옵션으로 직접 계산 가능) | 불필요 | – | 실시간 | – | – |
| I. 공매도·대차·ETF 설정환매 | 일별이라 4분 예측에 직접 효과는 작다. 국면 피처로 쓴다. | KIS 공매도 일별 `FHPST04830000`, 대차 `HHPST074500C0`, 신용잔고[22]. KRX 정보데이터시스템 | 불필요 | – | 일별 | – | ETF 설정환매는 상장좌수 변동으로 간접 추정한다. |

## 유료 구매 우선순위

1. **KIS 일본 실시간 (C1).** 월 수백 엔 수준으로 예상되고 도쿄 거래 시간이 KRX와 거의 겹친다. 일본 장비주가 우리 종목보다 먼저 움직이는지 시험해 볼 가치가 있다.
2. **KIS CME 실시간 MNQ/MES (A).** 거래소 원가는 월 몇 달러다. 하지만 Non-Display 라이선스 해당 여부를 먼저 확인해야 한다. 그때까지 보류한다. Databento($199)는 과하다.
3. **IBKR.** KIS가 HKFE·OSE·SGX 시세를 주지 않을 때만 고려한다. 월 약 $10이고 잔고 $500과 TWS/Gateway 상시 실행이 필요하다.
4. **과거 L2 (E).** 먼저 2–3개월 직접 녹화한다. 호가 피처가 유용하다고 입증되면 KRX 과거 데이터 구매를 검토한다.
5. **구매하지 않음.** 원·달러 현물 단말기, 뉴스 단말기, Bloomberg, 대만 피드. 비싸거나 구하기 어렵고, KIS 달러선물·DART·KIS 뉴스로 대부분 대체된다.

## 확인되지 않은 항목

- KIS의 CME·SGX·일본·홍콩 시세 실제 월 청구액. KIS는 가격표를 공개하지 않는다 (1577-1282 또는 HTS [7781]).
- 개인 예측 엔진이 CME의 non-display / semi-automated 사용에 해당하는지.
- KIS의 HKEX·OSE·Eurex 선물 무료 시세가 실시간인지.
- KIS `RBAQ` 시세가 어느 거래소를 집계하는지, 서머타임 기간 KIS 주간거래 정확한 시간.
- KIS나 키움에서 VKOSPI 코드를 받을 수 있는지.
- KRX·코스콤·Tick Data·LSEG 과거 데이터 가격과 개인 구매 가능 여부.
- 인포맥스·CHECK·마켓포인트·뉴스핌 개인 가격, OpenDART 일일 호출 한도.
- TAIFEX 수수료 면제, OSE $1 비전문가 요금(2021년 자료), Massive의 야간 세션 포함 여부.
- 2027-03 이후에도 개인이 증권사를 통해 NXT 데이터를 무료로 받을 수 있는지.

## 출처

1. https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/overseas_futureoption/overseas_futureoption_functions_ws.py
2. https://www.cmegroup.com/market-data/files/june-2026-market-data-fee-list.pdf (검색 요약으로 확인, PDF 직접 열람 못 함)
3. https://www.interactivebrokers.com/ibis/marketData.php
4. https://databento.com/pricing · https://databento.com/blog/updates-to-subscription-pricing
5. https://www.ampfutures.com/trading-info/exchange-data-fees
6. https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-subscription-minimum-equity-balance-requirements
7. https://cmegroup.com/market-data/distributor/files/cme-group-data-licensing-policy-guidelines-and-non-display-licensing-faq-october-2024.pdf
8. https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/overseas_stock/overseas_stock_functions_ws.py
9. https://www.smarttoday.co.kr/ko-kr/articles/95262 · https://v.daum.net/v/K5h8TeFYG2
10. https://www.businesswire.com/news/home/20230313005311/en · https://www.marketsmedia.com/blue-ocean-integrates-overnight-data-into-nyse-cloud-streaming
11. https://www.pyth.network/blog/blue-ocean-ats-joins-pyth-network-institutional-overnight-hours-us-equity-data
12. https://massive.com/pricing
13. https://www.waterstechnology.com/data-management/7953016/equity-data-plans-eye-dec-6-for-overnight-trading-launch
14. https://home.imeritz.com/fstock/ForeignStock01_02.do
15. https://www.phillipnova.com.sg/?p=12374
16. https://www.twse.com.tw/zh/products/information/qa.html
17. https://www.phillipnova.com.sg/?p=11301
19. https://apps.apple.com/us/app/id433844281
20. https://www.fnnews.com/news/202607071538355375
21. https://eulerpool.com/blog/bloomberg-terminal-cost
22. https://github.com/koreainvestment/open-trading-api/tree/main/examples_user/domestic_stock
23. https://sateconomy.co.kr/news/view/1065597321539758
24. https://www.segye.com/newsView/20260505509201
25. https://waterstechnology.com/node/2196121
26. https://www.lseg.com/en/data-analytics/market-data/data-feeds/tick-history
27. https://developer.ice.com/fixed-income-data-services/catalog/korea-exchange-krx
28. https://algolab.co.kr/blog/ls-openapi-news-telegram-bot
29. https://algolab.co.kr/blog/kis-api-realtime-nxt-krx-unified-tr-2026
30. https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_futureoption/domestic_futureoption_functions_ws.py
