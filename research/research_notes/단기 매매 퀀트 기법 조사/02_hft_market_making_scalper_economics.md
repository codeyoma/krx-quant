# HFT 전략·market-making 모델과 스캘핑의 실제 손익 구조 (전문가 vs 개인)

> 기준일 2026-10-17. 연구 정리이며 투자 조언이 아니다. 각 논문의 표본 기간을 함께 적었다. 2015년 이전 데이터는 "오래된 결과"로 읽어야 한다.
> 표기: (미확인)은 이번 조사에서 원문을 열어 확인하지 못한 항목이다. (2차)는 원문이 아니라 보도나 요약에서 가져온 항목이다.

## 1. Market-making 모델: 재고 위험, adverse selection, queue 위치 가치

### Takeaway
교과서 모델은 모두 같은 거래를 수식으로 푼다. market maker는 spread를 벌고, 그 대가로 재고(inventory) 위험과 informed flow에 당하는 adverse selection을 진다. 실무 수익은 queue 앞자리(price-time priority)와 빠른 cancel에 크게 기대며, 이 둘은 retail latency로는 얻을 수 없다.

### Cited Findings
- **Avellaneda & Stoikov (2008)**, "High-frequency trading in a limit order book", *Quantitative Finance* 8(3):217–224. Ho–Stoll dealer 모델을 stochastic control 문제로 옮겼다. mid price는 Brownian motion을 따른다. 시장가 주문은 호가 거리 δ에 따라 A·exp(−κδ) 강도로 도착한다. 재고가 쌓이면 reservation price를 반대 방향으로 옮겨 재고를 줄인다. 이후 market-making 연구의 기준 모델이다 — [NYU PDF](https://math.nyu.edu/~avellane/HighFrequencyTrading.pdf), DOI [10.1080/14697680701381228](https://doi.org/10.1080/14697680701381228) (DOI 자체는 미확인)
- **Guéant, Lehalle & Fernandez-Tapia (2013)**, "Dealing with the Inventory Risk. A solution to the market making problem". 재고 상한이 있을 때 HJB 방정식이 선형 ODE 시스템으로 줄어든다. 그래서 최적 호가를 풀 수 있고 closed-form 근사도 나온다 — [arXiv:1105.3115](https://arxiv.org/abs/1105.3115), DOI [10.1007/s11579-012-0087-0](https://doi.org/10.1007/s11579-012-0087-0). 게재지는 *Mathematics and Financial Economics*로 알고 있으나 arXiv 페이지에는 나오지 않는다 (미확인).
- **Cartea, Jaimungal & Penalva (2015)**, *Algorithmic and High-Frequency Trading*, Cambridge University Press, ISBN 9781107091146. 대량 주문 집행, market making, VWAP 추종, pairs, dark pool을 다룬다. 모델의 축은 "더 많이 아는 상대와 거래하는가"(adverse selection)이다 — [저자 companion 페이지](https://sebastian.statistics.utoronto.ca/?p=310), [도서 정보](https://www.booksamillion.com/p/Algorithmic-High-Frequency-Trading/lvaro-Cartea/9781107091146)
- **Moallemi & Yuan**, "A Model for Queue Position Valuation in a Limit Order Book" (Columbia, 초판 2016-12, 개정 2017-06). price-time priority 아래서는 앞선 주문이 먼저 체결되므로 queue 앞자리를 두고 경쟁한다. 위치 가치의 static 성분은 spread 수익과 adverse selection 비용의 차이다. adverse selection 비용은 queue 뒤로 갈수록 커진다 — [Columbia PDF](https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf) (Columbia 테스트 도메인 사본), [MIT ORC 세미나 초록](https://web.mit.edu/orc/www/archive/seminars/2014s/abs/Moallemi_abs.html)
- **Brogaard, Hendershott & Riordan (2014)**에 따르면 HFT의 liquidity-supplying 주문은 adverse selection을 당한다. 그래도 spread 수익과 liquidity rebate가 그 비용보다 크다 — [ECB WP 1602](https://ecb.eu/pub/pdf/scpwps/ecbwp1602.pdf), DOI [10.1093/rfs/hhu032](https://doi.org/10.1093/rfs/hhu032)

### Inferences
- 모델이 돈을 버는 조건은 queue 앞자리, 빠른 cancel(stale quote 회피), 낮은 fee와 rebate다. retail 계좌로 지정가를 걸면 queue 뒤에 서게 된다. Moallemi–Yuan 구조에서는 이 자리가 adverse selection이 가장 큰 자리다. 즉 "가격이 나에게 불리하게 올 때만 체결"되는 문제가 생긴다.
- A–S/GLFT의 inventory skew 아이디어(재고가 쌓이면 호가를 기울임)는 체결 엔진이 없어도 쓸 수 있다. "지금 들어가면 몇 분 안에 불리한 쪽으로 밀릴 확률"을 따로 추정하는 데 참고가 된다.

### Gaps
- Moallemi–Yuan 모델의 두 번째(dynamic) 성분과 "queue 가치가 half-spread와 같은 order"라는 정량 결론은 원문을 열지 못해 확인하지 못했다.
- KRX에 maker rebate나 maker/taker 차등 수수료가 있는지는 찾지 못했다. 개인 증권사 수수료는 편도 정률이다(프로젝트 설정 `KRX_FEE_PERCENT`, 기본 0.015%).

## 2. Latency arbitrage: 메커니즘과 규모

### Takeaway
latency arbitrage는 공개 정보에 가장 먼저 반응하는 쪽이 이기는 경주다. 이기는 차이는 microsecond 단위이고, 소수 회사가 대부분을 가져간다. 50–300 ms인 retail latency와는 4–5 자릿수 차이가 나므로 참가 자체가 불가능하다.

### Cited Findings
- **Budish, Cramton & Shim (2015)**, "The High-Frequency Trading Arms Race: Frequent Batch Auctions as a Market Design Response", *QJE* 130(4). 사람 시간 단위에서는 잘 맞는 상관(ES 선물–SPY)이 millisecond 단위에서는 완전히 깨진다. 이 틈이 가장 빠른 쪽만 먹는 mechanical arbitrage를 만든다. 기회의 지속시간 중앙값은 2005년 97 ms에서 2011년 7 ms로 줄었지만 수익성은 그대로였다. 대안으로 0.1초마다 여는 uniform-price frequent batch auction을 제안했다 — [UMD publication profile](https://econ.umd.edu/publicationprofile/2087), [NBER conference paper](https://conference.nber.org/conf_papers/f69665.pdf). DOI 10.1093/qje/qjv027은 미확인이다. (데이터 2005–2011, 오래된 결과)
- **Aquilina, Budish & O'Neill (2022)**, "Quantifying the High-Frequency Trading 'Arms Race'", *QJE* 137(1):493–564. LSE message data(실패한 주문과 cancel 포함)를 썼다 — [NBER w29011](https://www.nber.org/papers/w29011), DOI [10.1093/qje/qjab032](https://doi.org/10.1093/qje/qjab032)
  - FTSE 100 종목마다 race가 약 1분에 1번 일어난다. race의 최빈 길이는 5–10 μs다.
  - race가 전체 거래량의 약 20%를 차지한다. 상위 6개사가 race 승패의 80% 이상에 관여한다.
  - race 1회의 가치는 평균 약 half-tick이다. race가 price impact와 effective spread의 약 1/3을 설명한다.
  - latency arbitrage는 거래에 약 0.5 bp의 "세금"처럼 작동한다. 이것을 없애는 시장 설계는 유동성 비용을 약 17% 줄인다. 세계 주식시장 전체로 연 50억 달러 규모다.

### Inferences
- race가 없애는 것은 stale quote다. 느린 지정가 주문은 이 race의 먹잇감이 되기 쉽다. retail이 지정가로 "spread를 벌겠다"는 전략은 구조적으로 불리하다.
- race 1회의 가치가 half-tick 수준이라는 점도 중요하다. 정보가 0.5 tick짜리 이득이면 수수료와 세금을 내는 retail에게는 의미가 없다.

### Gaps
- KRX에서 latency race를 측정한 연구는 찾지 못했다. KRX 메시지 데이터로 Aquilina식 분석을 한 공개 연구는 이번 조사 범위에서 보이지 않았다.

## 3. HFT 수익성과 집중도

### Takeaway
HFT는 Sharpe가 매우 높은 사업이다. 다만 수익은 가장 빠른 소수에게 몰리는 winner-takes-all 구조이고, 신규 진입자는 덜 벌고 더 자주 퇴출된다. 공격적(liquidity-taking) HFT가 수동적 HFT보다 더 번다.

### Cited Findings
- **Baron, Brogaard & Kirilenko (2014 CFTC draft)**는 E-mini S&P 500 선물 2년치를 분석했다 — [CFTC PDF](https://www.cftc.gov/sites/default/files/idc/groups/public/@economicanalysis/documents/file/oce_riskandreturn0414.pdf), [SSRN 2433118](http://ssrn.com/abstract=2433118). (오래된 결과)
  - 중앙값 HFT 회사의 연율 Sharpe는 4.3이고 4-factor 연율 alpha는 22.02%다. 증거금 없이 전액을 투입했다고 가정한 값이라 실제 자본수익률보다 보수적이다.
  - Aggressive HFT의 평균 alpha는 90.67%로 Passive HFT(23.22%)보다 훨씬 높다.
  - Aggressive HFT는 짧은 시간 단위에서는 잃는다(spread와 impact). 대신 더 긴 장중 시간 단위에서 가격을 예측해 번다. Passive HFT는 그 반대다.
  - Aggressive HFT 매출의 약 45%는 다른 HFT 유형을 adverse-select해서 번 것이다.
  - 매출은 상위 회사에 지속적으로 쏠린다(Herfindahl 기준). 신규 진입자는 덜 벌고 더 많이 퇴출된다.
- **Baron, Brogaard, Hagströmer & Kirilenko (2019)**, *JFQA* 54(3):993–1024. 상대 latency 순위가 HFT 회사 간 성과 차이의 큰 부분을 설명한다. colocation 업그레이드로 순위가 오르면 성과도 좋아진다. 속도가 이익으로 이어지는 경로는 short-lived information과 risk management다 — [IDEAS/RePEc](https://ideas.repec.org/a/cup/jfinqa/v54y2019i03p993-1024_00.html). DOI 10.1017/S0022109018001096은 Cambridge 식별자에서 추정한 것이다 (미확인).
- **Brogaard, Hendershott & Riordan (2014)**, *RFS*. HFT의 liquidity-demanding 주문은 영구적 가격 변화 방향으로, 일시적 pricing error의 반대 방향으로 거래한다. HFT 주문 방향은 초(seconds) 단위의 가격 변화를 예측한다. 그 방향은 공개 정보(거시 발표, 시장 전체 움직임, LOB imbalance)와 상관이 있다 — [ECB WP 1602](https://ecb.eu/pub/pdf/scpwps/ecbwp1602.pdf), DOI [10.1093/rfs/hhu032](https://doi.org/10.1093/rfs/hhu032). (NASDAQ HFT 데이터 2008–2009로 알고 있음, 미확인. 오래된 결과)
- **Virtu Financial S-1 (2014)**: 2009-01-01부터 2013-12-31까지 1,238 거래일 중 손실일은 하루뿐이었다. 회사는 이를 실시간 risk management 덕분이라고 설명했다 — [SEC EDGAR S-1](https://www.sec.gov/Archives/edgar/data/0001592386/000104746914002070/a2218589zs-1.htm)

### Inferences
- "거의 매일 이기는" HFT의 수익은 거래 1건당 아주 작은 edge를 엄청난 거래 수로 곱한 결과다. 수수료 구조(rebate)와 속도 순위가 그 edge의 전제다. retail이 같은 규칙으로 거래 횟수만 늘리면 edge보다 비용이 먼저 커진다.
- Baron의 "Aggressive HFT는 짧은 단위에서 잃고 장중 긴 단위에서 번다"는 결과는 시사점이 있다. 방향을 맞히는 이익은 초 이하가 아니라 더 긴 시간 단위에서 나온다는 뜻이다. 다만 그 HFT들도 속도 우위를 함께 쓴다.

### Gaps
- Flow Traders 등 다른 회사 공시는 이번 조사에서 확인하지 않았다.
- 2020년대 HFT 업계 전체 수익(예: 업계 매출 추정)을 다룬 신뢰할 만한 1차 자료는 찾지 못했다.
- Virtu의 유일한 손실일이 실제 시장 손실인지 오류인지를 다룬 논의는 2차 자료뿐이라 넣지 않았다.

## 4. 개인 day trader·scalper의 실제 성과

### Takeaway
대만, 브라질, 한국 모두 결론이 같다. 개인 day trader 대다수는 비용을 빼면 잃는다. 지속적으로 이기는 개인은 1–3% 미만이다. 그 소수는 대체로 빠른 자동화, 짧은 보유, 단기 반전(contrarian) 전략, 낮은 비용 상품을 쓰는 사람들이었다.

### Cited Findings
**대만 (1992–2006, 오래된 결과)**
- **Barber, Lee, Liu & Odean (2014)**, "The Cross-Section of Speculator Skill: Evidence from Day Trading", *Journal of Financial Markets*. fee를 빼고도 예측 가능하게, 안정적으로 양의 abnormal return을 내는 day trader는 전체의 1% 미만이다 — [eScholarship](https://www.escholarship.org/uc/item/7k75v0qx), DOI 10.1016/j.finmar.2013.05.006
  - y년 성과 상위 500명은 y+1년에 하루 61.3 bp(fee 전), 37.9 bp(fee 후)를 번다(게재본). 2011년 working paper 수치는 49.5/28.1 bp라 판본마다 다르다 — [2011 WP](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf)
- **Barber, Lee, Liu, Odean & Zhang (2020)**, "Learning, Fast or Slow", *Review of Asset Pricing Studies* 10(1):61–93. 이전 제목은 "Do Day Traders Rationally Learn About Their Ability?"다 — [IDEAS/RePEc](https://ideas.repec.org/a/oup/rasset/v10y2020i1p61-93..html), [2018 WP](https://www.econ.puc-rio.br/uploads/adm/seminarios/Learning_fast_or_slow_180606.pdf)
  - day trader 전체의 성과는 음수다.
  - day trading 거래량의 74%는 손실 이력이 있는 사람들이 만든다.
  - day trader의 97%는 앞으로도 잃을 가능성이 높다.
  - 손실자는 수익자보다 더 잘 그만두지만, 학습이 느려 손실 이력이 있어도 계속한다.

**브라질 (2013–2015 시작자)**
- **Chague, De-Losso & Giovannetti (2019)**, "Day Trading for a Living?". 미니 Ibovespa 선물에서 day trading을 시작해 300일 이상 계속한 개인을 추적했다.
  - 97%가 손실을 냈다.
  - 은행 창구직원 일당(US$54)보다 많이 번 사람은 0.4%였다.
  - 최고 수익자는 하루 US$310을 벌었지만 표준편차가 US$2,560이었다.
  - 학습 효과의 증거는 없었다 — [IDEAS/RePEc (USP WP 2019_47)](https://ideas.repec.org/p/spa/wpaper/2019wpecon47.html), [SSRN 3423101](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101). SSRN은 이번에 403으로 열리지 않았다. 처음 day trading한 사람 전체가 19,646명이라는 수치는 검색 요약(2차)에서 나온 것이다.

**한국**
- **자본시장연구원(김민기) 2021 웨비나**. 증권사 4곳의 고객 204,004명(신규 60,446명)을 2020년 3–10월 동안 봤다.
  - 46%가 손실이었다(신규 투자자 62%, 기존 투자자 39%).
  - 회전율, 장중매매 비중, 종목 교체가 높을수록 일별 초과수익이 낮았다.
  - 출처: [파이낸셜뉴스 2021-04-14 (2차)](https://www.fnnews.com/news/202104131912345956). 보고서 원문은 미확인이다.
- **박수철·우민철 (2023)**, 「고빈도 알고리즘을 이용한 개인투자자의 투자성과」, 『재무관리연구』 40(5):1–28. 2018–2022년 KRX 전 종목을 분석했다 — [PDF (smallake.kr 사본)](https://smallake.kr/wp-content/uploads/2024/06/40-5-01_%eb%b0%95%ec%88%98%ec%b2%a0_%ec%9a%b0%eb%af%bc%ec%b2%a01-28.pdf)
  - "HFT 개인"은 한국거래소 알고리즘계좌로 등록한 개인으로 정의했다. 1주라도 거래한 개인 14,106,095계좌 중 628계좌다.
  - 이들은 수수료와 거래세를 빼고도 통계적으로 유의한 양의 수익을 냈다.
  - 주로 짧은 day trading을 하며, 일반 개인과 같은 역추세(contrarian) 전략을 쓴다. 과거에 떨어진 종목을 당일 사고 오른 종목을 당일 판다. 단기 추세의 반전을 노린다.
  - 순매수 포지션(NET(B))은 음수였다. 순매도 포지션(NET(S))과 day trading은 양수였다. 코스닥은 유가증권시장보다 성과가 약했다.
- **같은 논문의 국내 선행연구 요약**(위 PDF 5–7쪽, 원 논문들은 미확인):
  - 이은정 외(2007): 2003년 day trading 평균 −0.81%. 주된 원인은 당시 0.3% 거래세였다.
  - 우민철·최혁(2012): ETF day trading은 수익률로 약 +0.12%였다. ETF는 거래세가 면제되기 때문이며, 세금을 넣으면 손실로 바뀐다.
  - 류두진(2012): 파생상품 개인 day trader는 하루 2회까지만 이익이고, 그 이상 매매하면 손실이었다.
  - 이은정(2015): KOSPI200 선물의 개인 고빈도거래자는 건당 61,952원 손실이었다.
  - 박수철(2020): 주식 고빈도 165계좌는 +0.22%, ETF는 −0.04%, ELW는 −0.38%였다.
  - 우민철(2023): 초당 100건 이상 주문하는 82계좌 중 개인은 금액으로는 손실(117,331원)이었지만 수익률로는 +0.08%였다.

**crypto**
- **BIS Bulletin No 69 (2023-02)**, "Crypto shocks and retail losses". 2015-08부터 2022-12까지 거의 모든 국가에서 crypto 앱 사용자 다수가 bitcoin으로 손실을 봤다. Terra/FTX 때 대형 보유자는 팔고 소액 개인은 샀다 — [BIS](https://www.bis.org/publ/bisbull69.htm). 중앙값 투자자가 $900 중 $431을 잃었다는 수치는 [TheFullFX (2차)](https://thefullfx.com/bis-study-finds-retail-insto-divide-in-crypto-post-ftx/)에서 왔다.

### Inferences
- 사전확률(base rate)은 극히 낮다. 대만 <1%, 브라질 0.4%, 한국 알고리즘 등록 개인 628/1,400만이다. "백테스트에서 이긴다"만으로는 이 사전확률을 뒤집기 어렵고, 표본 밖 지속성(persistence) 검증이 핵심이다. Barber식 "y년 순위 → y+1년 성과" 검증이 그 예다.
- 한국에서 이긴 소수의 특징은 다음과 같다. ① 자동화와 빠른 주문, ② 짧은 보유(당일 청산), ③ 단기 반전(contrarian), ④ 매수보다 매도 쪽 타이밍, ⑤ 거래세가 없는 상품(ETF, 파생)에서 비용이 유리함. 이 가운데 ③④⑤는 신호 엔진이 직접 쓸 수 있는 정보다.
- 거래세는 국내 day trading 손익을 가르는 주요 변수로 반복해서 나온다(2007 0.3%, 2012 ETF 면제 효과).

### Gaps
- 자본시장연구원의 day trading이나 단타 전용 보고서(일 단위 회전 개인의 손익 분포)는 찾지 못했다. 김준석 연구위원 이름으로 된 관련 보고서도 확인하지 못했다.
- crypto scalping(초단타)만 따로 본 학술 증거는 찾지 못했다. BIS 자료는 보유 손익이다.
- 2023년 이후 한국 개인 day trader의 손익을 다룬 공개 통계는 찾지 못했다.

## 5. retail latency(50–300 ms)에서 남는 edge와 비용

### Takeaway
microsecond에서 millisecond 단위 경쟁(latency arb, queue 앞자리 market making, stale quote 공략)은 불가능하다. 순수 LOB/order-flow 신호의 예측력도 몇 번의 가격 변화 안에 사라진다. 남는 것은 몇 분 이상 지속되는 신호에 대한 방향 판단이다. 이 경우 예상 이동폭이 왕복 비용(KOSPI 매도세 0.20% + 수수료 + spread)을 넉넉히 넘어야 한다.

### Cited Findings
- latency의 크기: latency race는 5–10 μs에서 결판나고([Aquilina et al.](https://www.nber.org/papers/w29011)), 차익 기회의 중앙값은 2011년에 7 ms였다([Budish et al.](https://econ.umd.edu/publicationprofile/2087)).
- **Kolm, Turiel & Westray (SSRN 2021-08; 학술지 게재 정보는 미확인)**, "Deep Order Flow Imbalance: Extracting Alpha at Multiple Horizons from the Limit Order Book". NASDAQ 115종목을 분석했다. order flow(정상화 입력)로 학습한 모델이 원시 order book으로 학습한 모델보다 낫다. 종목별 예측의 유효 horizon은 약 "평균 가격 변화 2번"이다. information-rich 종목일수록 더 잘 맞는다 — [SSRN 3900141](https://papers.ssrn.com/abstract=3900141)
- **Brogaard et al. (2014)**: HFT 주문 방향의 예측력은 초 단위다 — [ECB WP](https://ecb.eu/pub/pdf/scpwps/ecbwp1602.pdf)
- **Moallemi & Sağlam (2013)**, "OR Forum—The Cost of Latency in High-Frequency Trading", *Operations Research* 61(5):1070–1086. latency 비용을 변동성과 bid-offer spread의 closed-form 함수로 냈다. NYSE 1995–2005에서 중앙값 latency 비용은 약 3배로 늘었다. latency는 HFT만이 아니라 모든 투자자의 거래비용 문제다 — [Columbia Business School](https://business.columbia.edu/faculty/research/cost-latency-high-frequency-trading), DOI [10.1287/opre.2013.1165](https://doi.org/10.1287/opre.2013.1165)
- **한국 거래세 2026**: 코스피는 매도금액의 0.20%(증권거래세 0.05% + 농어촌특별세 0.15%)다. 2026-01-01 양도분부터 적용됐고, 2025년에는 0.15%였다. 코스닥도 0.20%다 — [더팩트 2026-01-04](https://news.tf.co.kr/read/economy/2278747.htm), [세계일보 2026-01-05](https://www.segye.com/newsView/20260105516769). 코스닥 세율은 검색 요약(2차)에서 왔다.
- **ETF는 증권거래세가 면제된다.** 그래서 ETF day trading은 세전 손익과 세후 손익이 달라진다 — [박수철·우민철 2023, 선행연구 요약](https://smallake.kr/wp-content/uploads/2024/06/40-5-01_%eb%b0%95%ec%88%98%ec%b2%a0_%ec%9a%b0%eb%af%bc%ec%b2%a01-28.pdf)
- **호가단위**: 2023-01-25부터 코스피·코스닥·코넥스가 통일됐다. 10만–20만원 구간은 500원에서 100원으로 줄었다 — [MTN 2022-11-01](https://news.mtn.co.kr/news-detail/2022110114103274390)
- **krx-quant 자체 비용 모델**: 왕복 비용은 매도세 0.20% + 수수료 0.015% × 2 = 0.23%다.
  - 예: 81,250원(매수 1호가 81,200, 매도 1호가 81,300, k = 2)에서 비용은 약 1.87 tick이다.
  - 위 목표에 닿아 팔아도 1.5 − 1.87 = −0.37 tick이다. "맞혔는데 손해"인 셈이다 — `docs/engine-api.md`(닿음 손익 항목), `docs/design.md`

### Inferences
- 대략적인 손익분기 계산 (추론): 8만원대 종목에서 1 tick(100원)은 약 12.5 bp다.
  - 시장가로 들어가고 나오면 spread(1 tick)와 비용(약 1.9 tick)으로 거래당 약 3 tick을 쓴다.
  - 따라서 방향을 맞혔을 때 평균 이동이 3 tick을 넘지 못하면 hit rate가 높아도 손실이다.
  - 같은 계산에서 인버스 ETF 쪽은 0.20% 매도세가 빠진다. 대신 ETF 자체의 spread, 추적오차, 보수가 붙는다.
- Kolm의 "가격 변화 2번" horizon은 clock time이 아니라 event time이다 (추론). 005930처럼 tick이 크고 mid가 자주 안 바뀌는 large-tick 종목에서는 그 2번이 수십 초에서 몇 분일 수 있다. 4분 horizon은 순수 LOB 신호가 대부분 소진되는 경계에 있을 가능성이 크다.
- 사람이 신호를 보고 손으로 주문하는 구조라면 실효 latency는 50–300 ms가 아니라 수 초다 (추론). 이 지연에서 살아남는 신호여야 한다.
- 방향 판단 신호가 실제 손익으로 이어지려면 이동폭/비용 비율이 커야 한다. horizon을 늘리면(15분, 1시간, 종가, 며칠) 이동폭은 대략 √t로 커지고 비용은 고정이다. 그래서 비용 대비 신호의 경제성은 대체로 horizon이 길수록 나아진다 (추론, 신호 감쇠와 상충).

### Gaps
- KIS Open API의 실측 지연(체결/호가 push 지연, 주문 왕복)에 대한 공개 측정 자료는 찾지 못했다.
- KRX 대형주의 "가격 변화 2번"이 clock time으로 몇 초인지 다룬 연구는 찾지 못했다. 프로젝트 데이터로 직접 재야 한다.
- 인버스 ETF의 장중 spread와 괴리(거래 비용)에 대한 최신 공개 수치는 확인하지 않았다.

## 6. 최근 동향 (2023–2026): speed bump, PFOF, 한국 HFT 규제

### Takeaway
규제와 시장 설계는 느린 유동성 공급자를 latency arb에서 보호하는 쪽(asymmetric speed bump)과 retail 주문흐름의 이해상충을 줄이는 쪽(EU PFOF 금지)으로 움직였다. 미국은 2025년에 retail 주문 경쟁 규칙 제안을 철회했다. 한국은 2023년부터 고빈도 알고리즘 거래자 등록제를 운영한다.

### Cited Findings
- **Eurex Passive Liquidity Protection (PLP)**: 2019년에 도입한 asymmetric speed bump다. 2022-02부터 모든 주식·지수 옵션으로 넓혔다.
  - Le Moign(2024)의 diff-in-diff 분석에서 Eurex 유동성이 좋아졌다. 유동성 공급자에 대한 adverse selection이 줄었고, 경쟁 시장의 유동성도 함께 개선됐다 — [Institut Louis Bachelier PDF](https://www.institutlouisbachelier.org/wp-content/uploads/2024/03/speedbump-ilb.pdf)
  - DAX 옵션에서는 aggressive 주문에 1.5 ms 지연을 준다 — [Optiver](https://www.optiver.com/insights/markets/passive-liquidity-protection)
- **미국**: SEC는 2022-12에 Order Competition Rule(Reg NMS Rule 615, retail 시장가 주문을 짧은 공개 auction에 노출)을 제안했다. Atkins 위원장 아래에서 Gensler 시기 제안 14건과 함께 2025년에 철회됐다. Citadel Securities, Virtu, SIFMA는 반대했었다 — [GlobalTrading](https://www.globaltrading.net/?p=38776), [Wikipedia: PFOF](https://en.wikipedia.org/wiki/Payment_for_order_flow) (2차). 정확한 철회 날짜는 미확인이다.
- **EU**: Regulation (EU) 2024/791(MiFIR 개정)로 PFOF를 일반 금지했다. PFOF가 흔한 회원국은 자국 고객에 한해 2026-06-30까지 유예할 수 있었고, 독일이 이 유예를 썼다 — [PwC Legal](https://legal.pwc.de/en/news/articles/mifir-mifid-ii-review-making-sense-of-the-key-amendments), [독일 재무부 2024-03-21](https://www.bundesfinanzministerium.de/Content/DE/Pressemitteilungen/Finanzpolitik/2024/03/2024-03-21-weiterfuehrung-payment-for-order-flow-praxis.html)
- **한국**: 고빈도 알고리즘 거래자 등록제를 2023-01-25에 시행했다. 3개월 유예 뒤 2023-04-25부터 등록이 의무다. 거래소가 거래자별 식별코드를 부여해 감시하고, 증권사는 주문 시스템을 사전 점검할 의무를 진다 — [금융위원회](https://www.fsc.go.kr/po010101/79339), [머니투데이 2023-01-27](https://www.mt.co.kr/stock/2023/01/27/2023012715350053929)
  - 2023-01 증권선물위원회는 해외 소재 증권사(보도상 Citadel Securities)의 고빈도 매매에 시장질서 교란 과징금 118.8억원을 의결했다. 국내 첫 사례다 — [블록미디어 (2차)](https://www.blockmedia.co.kr/archives/290260)
  - 2026-04 보도는 국내에 HFT를 직접 규율하는 장치가 아직 부족하다고 지적했다 — [다음/언론 2026-04-06 (2차)](https://v.daum.net/v/20260406104301578)

### Inferences
- KRX에는 speed bump나 batch auction 같은 latency arb 완화 장치가 없는 것으로 보인다 (추론, 확인 못함). 연속 매매에서 느린 지정가는 여전히 stale quote 위험이 있다.
- PFOF와 retail 주문흐름 논쟁은 미국과 EU 이야기다. 국내 개인 주문은 거래소로 직접 가므로 이 프로젝트에 직접 영향은 작다.

### Gaps
- 2026-06-30 이후 EU PFOF 금지가 실제로 어떻게 적용됐는지는 확인하지 못했다.
- 2023–2026 사이 crypto 거래소의 개인 scalping 성과를 다룬 1차 연구는 찾지 못했다.
- 한국 HFT 등록 기준(초당 호가 건수 등)의 정확한 수치는 확인하지 못했다.

## 7. krx-quant 적용 메모

### Takeaway
retail 신호 엔진이 현실적으로 노릴 수 있는 것은 수 분에서 수일의 방향 판단이다. 거래세 0.20%를 고려하면 4분은 경계선이고, 15분–장중 종가–수일 horizon이 비용 대비 더 유리할 가능성이 높다. market making, latency arb, rebate capture, news latency 전략은 대상에서 빼는 것이 맞다.

### Cited Findings
- 이기는 쪽은 μs 속도, queue 앞자리, rebate를 가진 소수다 — [Aquilina et al.](https://www.nber.org/papers/w29011), [Baron et al. 2014](https://www.cftc.gov/sites/default/files/idc/groups/public/@economicanalysis/documents/file/oce_riskandreturn0414.pdf), [Baron et al. 2019](https://ideas.repec.org/a/cup/jfinqa/v54y2019i03p993-1024_00.html)
- 개인 day trader의 97% 이상이 잃는다 — [Chague et al.](https://ideas.repec.org/p/spa/wpaper/2019wpecon47.html), [Barber et al. 2020](https://ideas.repec.org/a/oup/rasset/v10y2020i1p61-93..html)
- 국내에서 이긴 소수는 자동화된 당일 contrarian 매매자였고, 매도 쪽 성과가 좋았다 — [박수철·우민철 2023](https://smallake.kr/wp-content/uploads/2024/06/40-5-01_%eb%b0%95%ec%88%98%ec%b2%a0_%ec%9a%b0%eb%af%bc%ec%b2%a01-28.pdf)
- LOB 신호의 수명은 가격 변화 몇 번 수준이다 — [Kolm et al.](https://papers.ssrn.com/abstract=3900141)
- 프로젝트 비용 모델에서 k = 2 tick 목표는 맞혀도 −0.37 tick이다 — `docs/engine-api.md`

### Inferences
- **현실적이지 않은 것**: market making(지정가로 spread 벌기), latency/cross-market arb(현물–선물 괴리를 ms 안에 잡기), rebate capture, 뉴스와 공시에 대한 속도 경쟁, 초 단위 scalping. 모두 retail latency와 매도세 0.20%에서 기대값이 음수일 가능성이 크다.
- **현실적인 것** (모두 추론, 검증 필요):
  1. **horizon을 늘린 방향 신호.** 4분 → 15/30/60분 → 종가 → 1–5일로 사다리를 만든다. 각 horizon에서 "평균 이동폭 ÷ 왕복 비용"과 hit rate를 같이 본다. 비용이 고정이므로 hit rate가 조금 떨어져도 이동폭이 커지면 순이익이 나아질 수 있다.
  2. **느린 정보 우위.** program 매매, 투자자별·회원사별 수급, 선물·옵션(basis, 미결제), 장중 VWAP 이탈의 회귀. 이것들은 μs 경쟁의 대상이 아니고 수 분에서 수일 지속될 수 있다. lead-lag(선물 → 현물)도 ms 단위 차익이 아니라 분 단위로 남는 부분만 의미가 있다.
  3. **단기 반전(contrarian) 계열**은 국내 HFT 개인의 성공 사례와 맞는다. VWAP reversion, 과도한 장중 이탈 후 회귀가 이에 해당한다.
  4. **하락 신호와 인버스 ETF**: 매도세 면제가 비용상 유리하다. 단 ETF의 spread, 괴리, 추적오차를 비용 모델에 따로 넣어야 한다. 현재 0.23% 공통 비용은 인버스 쪽 비용을 실제와 다르게 잡을 수 있다.
- **평가 방법**:
  1. hit rate만으로는 부족하다. "맞혔는데 손해"가 이미 문서화돼 있다. 각 horizon과 종류마다 순기대값(tick)과 거래 수를 1급 지표로 둔다.
  2. 알고리즘 순위의 표본 밖 지속성을 확인한다. 기간 A 순위 상위가 기간 B에도 상위인지 보는 Barber식 검증이다. 개인 대다수가 잃는다는 사전확률이 강하므로 이 검증이 필요하다.
  3. 실행이 사람 손이라면 신호 시각에서 수 초 뒤 가격으로 진입하는 지연 민감도 테스트를 넣는다(예: +1초, +5초, +30초).
- **scalping, day trading, swing 비교 요약**:
  - scalping(초–1분): retail로는 비현실적이다.
  - day trading(수 분–장중): 비용을 넘는 이동폭을 가진 일부 신호와 상황에서만 가능하다.
  - swing(수일): 비용 부담이 가장 작다. 대신 overnight 위험과 다른 정보원(수급 누적, 선물·옵션 포지션)이 중요해진다.

### Gaps
- KRX 3종목에서 horizon별 "이동폭 ÷ 비용" 비율과 신호 감쇠 곡선은 공개 자료로 알 수 없다. krx-quant 기록 데이터로 직접 측정해야 한다.
- 인버스 ETF(예: KOSPI200 인버스)로 개별 종목 하락을 대신 거래할 때의 basis 위험(종목 vs 지수)은 이 조사 범위 밖이다.
