# 해외 금융업계 자료로 본 단기 매매 기법과 시장 메커니즘 (보강 조사 13)

*기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다. 1차 보고서(`reports/단기 매매 퀀트 기법 조사.md`)가 이미 다룬 것(OFI, queue imbalance, Hawkes, intraday momentum와 Baltussen et al. 2021, 같은 시간대 periodicity, overnight 반전, 옵션 개장 10분 imbalance(Kang, Kang, Lee 2022), end-of-day reversal, ORB, short-term reversal, TSMOM, pairs, PEAD, S&P 편입 효과, Blitz et al. 2023, Man AlphaGPT, Citadel Securities 과징금, Aquilina·Budish·O'Neill 2022)은 반복하지 않고 필요할 때만 언급한다.*

*조사 방법: WebSearch 15회(끝까지 작동했고 quota 소진 없음), 나머지는 WebFetch. SSRN, ScienceDirect, NYU(Almgren), USI 페이지는 403/404로 막혔다. 막힌 원문은 abstract 페이지(IDEAS/RePEc 등)나 PDF를 내려받아 직접 텍스트를 뽑아 확인했다. 직접 확인하지 못한 수치는 "미확인"이나 "2차 인용"으로 표시했다.*

## 1. 옵션발 흐름: dealer gamma(GEX), pinning, 0DTE, vanna/charm, put-call ratio

### Takeaway
"딜러 gamma 부호가 장중 모멘텀/반전을 바꾼다"는 학술 근거(Barbon & Buraschi, Barbon et al.)가 있고, 만기일 pinning은 개별주와 S&P 500 선물 모두에서 실증됐다. 그러나 vendor식 GEX는 "고객은 call을 사고 put을 산다"는 부호 가정에 기대는데, 거래소가 직접 본 SPX 0DTE 고객 흐름은 거의 균형이었다. 그래서 딜러 순 gamma는 선물 유동성의 0.04~0.2%에 그쳤다. KOSPI200은 지수 만기 효과(Park & Lim 2003)만 확인됐고, KOSPI200 pinning이나 GEX를 직접 검정한 연구는 찾지 못했다.

### Cited Findings
- **Gamma Fragility** (Andrea Barbon & Andrea Buraschi, working paper, 최종 개정 2021-03-19): 사전 gamma imbalance가 음수이고 기초자산이 비유동적이면 장중 momentum이, 양수이면 장중 reversal이 나타난다. 효과는 가장 비유동적인 종목에서 가장 크고, gamma imbalance는 flash crash의 빈도·크기와 관련된다. 페이지에 계수는 없다 — [abarbon.com](https://abarbon.com/papers/gamma-fragility)
- **Liquidity Provision to Leveraged ETFs and Equity Options Rebalancing Flows: Evidence from End-of-Day Stock Prices** (Barbon, Beckmeyer, Buraschi, Moerke; 개정 2022-04-29, working paper). LETF 리밸런싱과 옵션 delta-hedging이 모두 장 마감 momentum/reversal을 만든다. 효과는 다음 거래일 안에 사라진다. gamma 효과는 표본 내내 지속됐고 LETF 효과는 시간이 갈수록 줄었다. 저자들은 LETF 흐름이 더 예측 가능해서 유동성 공급이 더 많이 붙은 결과로 해석한다 — [abarbon.com](https://abarbon.com/papers/liquidity-provision-to-leveraged-etfs-and-equity-options-rebalancing-flows), [SFI WP 22-40](https://www.sfi.ch/en/publications/n-22-40-liquidity-provision-to-leveraged-etfs-and-equity-options-rebalancing-flows-evidence-from-end-of-day-stock-prices)
- **Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)** (Barbon, Aziz, Zarattini; SFI WP 24-97, 2024-11-27). 2007~2024년 초 비용 차감 누적 1,985%, 연 19.6%, Sharpe 1.33. 딜러 gamma imbalance가 전략 수익의 변화를 예측하는지도 검정했지만, abstract는 결과를 밝히지 않는다. 실무자 공저 working paper이고 독립 재현은 찾지 못했다 — [SFI](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy)
- **Pinning in the S&P 500 futures** (Golez & Jackwerth, *JFE* 106(3), 2012). S&P 500 futures 옵션의 serial 만기일에 선물 가격이 ATM 행사가로 끌려간다. 지수 옵션 만기 직전에는 반대로 ATM 행사가에서 밀려난다(anti-cross-pinning). 명목가치 이동은 "만기일마다 최소 $115M"이다. 원인은 market maker의 delta hedge 재조정과 개인의 ITM 옵션 재매도·조기행사다 — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v106y2012i3p566-585.html)
- **Stock price clustering on option expiration dates** (Ni, Pearson, Poteshman, *JFE* 78(1), 2005). 제목대로 옵션 만기일 종가가 행사가 주변에 몰린다는 고전 연구다. abstract와 원문은 403으로 확인하지 못했다. 그래서 효과 크기와 원인(market maker hedge vs 증권사 자기매매의 조작) 비중은 **미확인**이다 — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v78y2005i1p49-87.html)
- Avellaneda & Lipkin, "A Market-Induced Mechanism for Stock Pinning": 특정 행사가의 open interest가 크면 floor market maker의 집단 delta-hedging이 주가를 행사가로 몰아 pinning 확률을 만든다는 모형이다(이론) — [NYU](https://math.nyu.edu/~avellane/stock_pinning.html)
- **Volatility Insights: Much Ado About 0DTEs** (Mandy Xu, Cboe, 2023-09-08). SPX에서 0DTE 비중은 2016년 약 5%에서 2023년 8월 평균 50%로 늘었다. market maker 순 gamma는 하루 평균 약 $170mm~$670mm로, S&P 선물 일 거래의 0.04~0.17%다. 2023-08-15 4440 put은 10만 계약 넘게 거래됐지만 market maker는 약 3천 계약 순매도로 끝났다(매수 52k 대 매도 55k). **거래소 자체 자료이고 이해당사자다** — [Cboe](https://www.cboe.com/insights/posts/volatility-insights-evaluating-the-market-impact-of-spx-0-dte-options)
- **0DTEs Decoded: Positioning, Trends, and Market Impact** (Mandy Xu, Cboe, 2025-05-02). 고객 흐름은 "extremely balanced"이고, 순 market maker gamma hedging은 많아야 SPX 일 유동성의 약 0.2%다. 0DTE 거래의 95% 이상은 손실 한정 구조이고 naked short는 약 4%다. 같은 이해당사자 단서가 붙는다 — [Cboe](https://www.cboe.com/insights/posts/0-dt-es-decoded-positioning-trends-and-market-impact)
- 0DTE 학술 검정(Dim, Eraker, Vilkov, "0DTEs: Trading, Gamma Risk and Volatility Propagation", SSRN 4692190): 원문은 403이었다. 2차 요약에 따르면 0DTE open-interest gamma가 지수 변동성을 키운다는 증거는 없다. 오히려 0DTE gamma가 높을수록 장중 실현 변동성이 낮았다 — [Harbourfront Quant 요약](https://harbourfrontquant.substack.com/p/the-rise-of-0dte-options-cause-for). 0DTE 거래일과 실현 변동성의 음의 관계를 market maker 순 gamma로 설명한 다른 연구의 요약도 있다 — [Quantpedia](https://quantpedia.com/do-sp500-0dtes-options-increase-market-volatility/). 둘 다 **2차 요약**이다.
- GEX·"gamma flip"·vanna·charm 흐름은 SpotGamma 같은 데이터 vendor가 퍼뜨린 개념이다. 이 조사에서 본 SpotGamma 자료는 Barbon & Buraschi 기사를 다시 실은 것이었다(**vendor/마케팅 자료, 본문 미확인**) — [SpotGamma](https://spotgamma.com/wall-street-dealers-in-hedging-frenzy-get-blamed-for-volatility/)
- **The Information in Option Volume for Future Stock Prices** (Pan & Poteshman, *RFS* 19(3), 2006). 신규 매수 개시(opening buyer-initiated) 거래량으로 만든 put-call ratio가 낮은 종목은 높은 종목보다 다음날 40bp 넘게, 다음 주 1% 넘게 오른다. 저자들은 이 예측력이 시장 비효율이 아니라 옵션 거래자의 **비공개 정보**에서 온다고 본다. 공개 put-call ratio로는 같은 결과를 기대할 수 없다는 뜻이다 — [IDEAS](https://ideas.repec.org/a/oup/rfinst/v19y2006i3p871-908.html)
- **Expiration Day Effects in Korean Stock Market: Wag the Dog?** (박창균·임경묵, KDI *Journal of Economic Policy* 25(2), 2003-12). KOSPI200 파생상품 만기일에 현물시장에서 "strong expiration day effects"가 나타났다. 저자들은 그 원인을 마지막 10분 call auction으로 정해지는 정산가격 절차에서 찾았다. 효과의 방향과 크기는 abstract에 없다 — [KDI JEP](https://kdijep.org/v.25/2/137/Expiration+Day+Effects+in+Korean+Stock+Market+Wag+the+Dog+)
- KOSPI200 weekly options: 매주 목요일 만기 상품을 도입한다는 보도가 2019-07-31에 나왔다 — [아주경제](https://www.ajunews.com/view/20190731181038818). Eurex는 "KOSPI 200 weekly options with **Monday expiry**"의 daily futures(OKM1~OKM5)를 2023-07-31부터 거래한다고 공지했다(Notice 048/23, 2023-06-19). KRX 월요일 만기 상품의 상장일 자체는 KRX 1차 자료로 확인하지 못했다 — [Eurex](https://www.eurex.com/ex-en/find/kr/news/etd-project-kr-3595508)
- **Investors' net buying pressure and implied volatility dynamics** (Ryu, Webb, Yang, Yu, *Borsa Istanbul Review* 22(4), 2022). KOSPI200 옵션에서 IV 변동에 가장 정보가 많은 것은 외국인 기관의 순수요였다. 국내 개인은 제도 개편 뒤에만 부분적으로 정보가 있었다. 방향보다 **변동성** 정보다 — [XJTLU](https://scholar.xjtlu.edu.cn/en/publications/investors-net-buying-pressure-and-implied-volatility-dynamics/)

### Inferences
- 학술 결과(Barbon 계열)는 "gamma 부호 × 비유동성"이 마감 근처 momentum/reversal의 **조건**을 바꾼다는 것이다. vendor처럼 GEX 수준 자체로 방향을 맞힌다는 주장과는 다르다. Cboe 자료를 보면 0DTE 쪽 딜러 부호를 가정으로 추정하면 틀리기 쉽다.
- KOSPI200은 cash-settled 지수 옵션이다. 그러니 개별주 pinning(Ni et al.)보다 Golez & Jackwerth형 **지수 수준 pinning**이 더 맞는 비교 대상이다. 삼성전자·SK하이닉스 개별 종목에서는 개별주 옵션보다 2026년 단일종목 LETF가 훨씬 큰 마감 흐름을 만든다(2절).
- 공개 put-call ratio는 Pan & Poteshman의 결과를 재현하지 못할 가능성이 크다. 그 예측력은 공개되지 않는 "개시 매수" 구분에서 나왔기 때문이다.

### Gaps
- KOSPI200 지수의 만기일 행사가 pinning, KOSPI200 딜러 gamma, KRX 0DTE·weekly의 장중 영향을 다룬 실증 연구는 찾지 못했다(영문·한글 검색 각 1회).
- vanna/charm 흐름, "max pain", gamma flip 수준의 예측력을 독립적으로 검정한 학술 연구는 찾지 못했다. vendor 주장으로만 남는다.
- Ni, Pearson, Poteshman(2005)과 Dim, Eraker, Vilkov의 수치는 원문 접근 실패로 미확인이다.
- Xing, Zhang, Zhao(2010)의 skew 기반 주간 예측 등 skew 신호 문헌은 이번에 확인하지 못했다.

## 2. 마감 동시호가와 지수·ETF 이벤트: closing auction, LETF 리밸런싱, 지수 리밸런싱, CTA

### Takeaway
2026년 KRX에서 가장 크고 가장 예측 가능한 장중 흐름은 **삼성전자·SK하이닉스 단일종목 LETF의 마감 리밸런싱**이다. 2026-05-27 상장 뒤 SK하이닉스의 의무 리밸런싱은 일 거래대금의 평균 22.4%(최대 50.4%)였다. 한 working paper는 이 흐름을 노린 선행 매매가 종가를 과도하게 밀고, 그 75%가 다음날 종가까지 되돌아간다고 보고했다. 미국에서도 closing auction의 가격 압력은 빠르고 거의 완전하게 되돌아갔다.

### Cited Findings
- **Preying on Leveraged ETFs** (Yinhong Zhao, Princeton; arXiv 2608.03703, v1 2026-08-04, v4 2026-08-29; 저자 표기 "preliminary version"; 비동료심사; 저자는 코딩·분석에 LLM의 도움을 받았다고 밝힘) — [arXiv](https://arxiv.org/abs/2608.03703). 아래 수치는 PDF 본문에서 직접 확인했다:
  - 2026-05-27 삼성전자·SK하이닉스를 기초로 한 단일종목 LETF 16개가 상장했다. 합산 자산은 KRW 4.3조로 시작해 3주 만에 14조가 됐다. 6주 안에 16개 모두 상장가 아래로 떨어졌고(최악 −43%), 보유자의 92%가 개인이었다.
  - 5/27 뒤 석 달 동안 KOSPI 서킷브레이커가 7번 발동했다. 지수의 평균 일 변동은 4.1%(2025년 1.0%)였고, 사이드카는 31번 발동했다(8/24까지).
  - SK하이닉스의 전 세계 의무 리밸런싱은 일 거래대금의 평균 22.4%, 최대 50.4%였다. 삼성전자의 상장 뒤 평균은 미국에서 가장 극단적인 MicroStrategy 수준이었고, SK하이닉스는 그 두 배를 넘었다.
  - 실행 구조: 국내 현물형 상품은 15:20~15:30 주식 동시호가에 맞춰져 있다. 선물형·인버스 상품은 15:45까지 거래되는 단일종목 선물로 리밸런싱한다. 홍콩 상장 swap형 상품도 계약상 한국 종가에 묶여 같은 동시호가에서 집행된다. KRX는 마감 창 동안 예상 체결가를 실시간으로 공개한다. 동시호가 VI가 걸리면 15:32까지 연장되는데, 61일 중 52일, 401개 종목-일이 영향을 받았다.
  - 전일 종가→15:20 수익률로 미리 정해지는 리밸런싱 수요를 동시호가 체결대금으로 나눈 값은 SK하이닉스에서 상장 뒤 중앙값 1.06, 최대 7.50이었다. 46일 중 26일은 1을 넘었다.
  - 장 시작 전 미국 시장 움직임을 공개 뉴스의 instrument로 쓰면, 상장 뒤 두 종목에서만 첫날 반응의 **평균 75%가 다음날 종가까지 반전**됐다. 반전도 과하게 일어나 며칠 동안 진동했다. 미국 지수·LETF 종목, 상장 전의 두 종목, 상장 뒤 다른 한국 종목 등 대조군에서는 반전이 없었다(difference-in-differences).
  - 모형 반사실 계산으로는 SK하이닉스 연환산 변동성이 100.0%에서 실제 136.7%로 커졌다. 9주 동안 KRW 1.38조가 상품 보유자에게서 빠져나갔고, 이는 초기 투자금의 17.6%다.
  - 정책: 2026-07-28 금융위원회가 운용사에 리밸런싱 시점을 앞당기고 분산하라고 요청했고, 업계 협회는 이틀 뒤 이를 채택했다. 저자의 시뮬레이션에서 분산은 오히려 변동성을 키웠다. 저자는 대안으로 시가 동시호가(overnight gap분)와 종가 동시호가(장중분)로 나눈 하루 2회 리밸런싱을 제안한다.
- **The Dynamics of Leveraged and Inverse Exchange-Traded Funds** (Minder Cheng & Ananth Madhavan, *Journal of Investment Management*, 2009). 매일 레버리지를 다시 맞추는 리밸런싱이 장 마감 무렵 변동성을 키울 수 있다. 장기 보유자에게는 경로 의존 옵션처럼 작동해 가치를 깎을 수 있다 — [JOIM](https://joim.com/dynamics-leveraged-inverse-exchange-traded-funds). 2차 인용으로 "지수가 하루 1% 움직이면 MOC 수요가 17%, 5%면 50% 늘어난다"는 추정이 있다(**원문 미확인**) — [FPA PDF](https://www.financialplanningassociation.org/sites/default/files/2021-08/JAN11%20Inverse%20and%20Leveraged%20ETFs%20-%20Considering%20the%20Alternatives.pdf)
- Zhao(2026)는 미국 지수·단일종목 LETF 연구(Ivanov & Lenkey 2018, Barbon et al. 2022, Lenkey 2024)를 이렇게 요약한다. 가격 영향은 "modest"했고, 예측 가능성이 높을수록 줄었다. Zhao는 그 차이가 상품 규모 대비 체결 시장 크기(loop gain)에서 온다고 본다 — [arXiv](https://arxiv.org/abs/2608.03703)
- **Who trades at the close? Implications for price discovery and liquidity** (Bogousslavsky & Muravyev, *Journal of Financial Markets* 66, 2023). 미국 closing auction은 2018년 일 거래량의 7.5%였다(2010년 3.1%). 종가는 대개 마감 직전 매수·매도호가와 같고, 가격 영향은 연속매매보다 작다. auction 대 장중 거래량 비율은 S&P 500 편입일에 급증하고 그 뒤 영구히 높아진다. 저자들은 indexing·ETF 성장을 원인으로 본다. auction 가격 이탈은 평균적으로 빠르고 거의 완전하게 되돌아간다 — [IDEAS](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html)
- **Two centuries of trend following** (Lempérière, Deremble, Seager, Potters, Bouchaud; arXiv 1404.3274, 2014): 2절과 4절 공통 근거다. 추세의 초과수익 t-stat은 1960년 이후 약 5, 1800년 이후 약 10이었다. 긴 추세는 최근에도 약해지지 않았지만 **짧은 추세는 "significantly withered"**였다 — [arXiv](https://arxiv.org/abs/1404.3274)

### Inferences
- LETF 리밸런싱 규모는 표준 계산으로 정해진다. 자산 A, 배수 L, 당일 수익률 r이면 리밸런싱 금액은 A·(L²−L)·r다. Zhao의 2배 예시(1% 상승 → 자산의 2% 추가 매수)와 맞는다. 그래서 2배와 −2배가 **모두 당일 움직임과 같은 방향**으로 사들이거나 판다. 15:20 이전에 장중 수익률로 거의 다 계산된다.
- 이 메커니즘의 예측은 두 방향이다. (a) 당일 마감 쪽은 같은 방향 압력이다. 다만 선행 매매자가 장 초반부터 미리 들어오므로 시점이 앞당겨진다. (b) 다음 거래일은 반전이다(75%). 4분 신호보다 `open_to_close`·`last30`·`gap` 류 예측 종류와 맞는다(1차 보고서 4~6순위).
- 2026-07-28~30의 리밸런싱 분산 지침은 마감 집중 패턴의 구조 변화점이다. 학습 표본을 5/27 이전, 5/27~7/30, 7/30 이후로 나눠 보지 않으면 신호가 섞인다.
- 지수 리밸런싱(KOSPI200 정기변경)과 ETF 설정·환매도 같은 "예측 가능한 마감 주문" 부류다. 다만 Zhao는 국내 LETF의 설정·환매(primary market)가 당일 수익률에 같은 날 반응하지 않는다고 보고했다(본문 Table A7).

### Gaps
- KOSPI200 정기변경(6월·12월) 마감 효과, ETF 설정·환매 흐름, 국내 KOSPI200 레버리지/인버스 ETF(지수형)의 마감 효과를 다룬 최근 한국 연구는 찾지 못했다.
- 금융위 분산 지침 뒤 실제 리밸런싱 시각(몇 시, 몇 분할)은 확인하지 못했다.
- Nasdaq Closing Cross·NYSE imbalance feed로 만든 imbalance 전략의 비용 차감 성과는 1차 자료를 찾지 못했다(Optiver Kaggle 대회는 1차 보고서가 다룸).
- 은행 desk의 CTA positioning 모델(예: 매수·매도 trigger 수준 추정)은 고객 전용 자료다. 공개 원문과 독립 검증을 찾지 못했다.

## 3. 계절성과 매크로 이벤트: pre-FOMC drift, 발표 시각, 월말·분기말, 연기금 리밸런싱

### Takeaway
업계와 중앙은행이 문서화한 달력 효과는 대부분 발표 뒤 약해졌거나(pre-FOMC drift는 2015년 이후 사라짐), 일 단위 효과다(리밸런싱 다음날 −17bp, 분기말 종가 부풀리기). 4분 방향보다는 일 단위 `gap`/`open_to_close` 예측 종류의 조건 변수에 가깝다.

### Cited Findings
- **The Pre-FOMC Announcement Drift** (David Lucca & Emanuel Moench, NY Fed Staff Report 512, 2011-09, 2013-08 개정; *JF* 2015). PDF 본문에서 확인한 수치는 이렇다. S&P 500은 예정된 FOMC 발표 전 24시간(전일 14시~당일 14시)에 평균 **49bp**(1994-09~2011-03, 131회; 1980년 이후 37bp) 올랐다. 다른 날 24시간 평균은 0.004%다. 최대 |t| 4.51은 bootstrap 분포의 99.98% 백분위보다 컸다. 같은 창의 평균은 DAX 0.449%, CAC 0.517%, FTSE 0.347%, SMI 0.301%, TSX 0.23%이고, **Nikkei는 0.006%로 사실상 0**이다(close-to-close) — [NY Fed](https://www.newyorkfed.org/research/staff_reports/sr512.html), [PDF](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr512.pdf). 이 drift가 같은 기간 미국 주식 프리미엄의 80% 이상이라는 수치는 2차 인용이다 — [NBER 회의 논문](https://conference.nber.org/conf_papers/f64395/f64395.pdf)
- **The Disappearing Pre-FOMC Announcement Drift** (Kurov, Wolfe, Gilbert, *Finance Research Letters* 40, 2021). 표본을 2019-12까지 늘리면 drift는 2015년 이후 기자회견이 있는 발표와 없는 발표 모두에서 "essentially disappeared"였다. 저자들은 불확실성 감소를 설명으로 든다 — [IDEAS](https://ideas.repec.org/a/eee/finlet/v40y2021ics1544612320315956.html)
- **The Unintended Consequences of Rebalancing** (Campbell Harvey, Michele Mazzoleni, Alessandro Melone; NBER WP 33554, 2025-03, 2026-01 개정). 기관의 달력 기반·이탈 기반 리밸런싱이 예측 가능한 가격 패턴을 만든다. 주식이 overweight일 때 다음날 주식 수익률이 **17bp** 낮았다. momentum, reversal, 매크로 정보를 통제해도 유지된다. 비용은 연 약 $16B(미국 가구당 약 $200)로 추정했고, 일부 참가자가 이를 front-run할 수 있다고 본다 — [NBER](https://www.nber.org/papers/w33554), [Duke Fuqua](https://www.fuqua.duke.edu/duke-fuqua-insights/what-do-pensions-lose-from-rebalancing)
- **Dash for Cash: Monthly Market Impact of Institutional Liquidity Needs** (Etula, Rinne, Suominen, Vaittinen, *RFS* 33(1), 2020). 월간 지급 주기가 전 세계 유동성 높은 시장에 체계적 패턴을 만든다. 핵심 날짜 전후에 부채·주식 자본비용이 일시적으로 오르고, 기관이 이를 만든다는 증거를 보였다. abstract에 효과 크기는 없다 — [IDEAS](https://ideas.repec.org/a/oup/rfinst/v33y2020i1p75-111..html)
- **Leaning for the Tape: Evidence of Gaming Behavior in Equity Mutual Funds** (Carhart, Kaniel, Musto, Reed, *JF* 57(2), 2002). 펀드매니저가 분기말에 이미 보유한 종목을 막판에 사들여 평가가격을 부풀린다. 부풀림은 대형주 펀드 연 약 0.5%에서 소형주 펀드 연 2% 이상이다. 분기 마지막 몇 분에 거래와 가격이 함께 급등한다 — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v57y2002i2p661-693.html)

### Inferences
- FOMC 발표는 14:00 ET, 즉 KST 03:00(서머타임) 또는 04:00다. 그래서 pre-FOMC 24시간 창은 발표 직전 한국 정규장 하루를 통째로 포함한다. 다만 원 논문에서 같은 아시아 시간대의 Nikkei에는 drift가 없었고, 미국에서도 2015년 이후 사라졌다. 사전확률은 낮다.
- 미국 CPI·고용지표(08:30 ET = KST 21:30/22:30)는 한국 장이 끝난 뒤 나온다. 그 영향은 다음날 시가 갭으로 들어간다. 1차 보고서의 `gap` 종류에 "전날 밤 미국 매크로 발표 여부" 플래그를 넣는 근거다(추론).
- 연기금 리밸런싱은 한국에서도 월말·분기말, 그리고 큰 등락 뒤의 이탈 기준으로 일어날 수 있다. 엔진의 투자자별 일별 수급 10년치(연기금 구분 포함 여부 확인 필요)로 Harvey et al.식 검정이 가능하다(추론).

### Gaps
- 한국 시장의 pre-FOMC drift, turn-of-month, 분기말 window dressing, 국민연금 리밸런싱의 가격 영향을 다룬 1차 연구는 이번 검색 한도 안에서 찾지 못했다.
- Savor & Wilson(2013) 등 매크로 발표일 프리미엄 문헌은 확인하지 못했다.
- Lucca & Moench의 80% 수치는 2차 인용이다.

## 4. 운용사·은행 리서치 라이브러리: Man, AQR, CFM, Graham, J.P. Morgan

### Takeaway
대형 systematic 운용사가 공개한 단기 관련 결론은 셋이다. (1) 짧은 추세는 오래 보면 약해졌고, 비용을 빼면 느린 추세보다 Sharpe가 낮다. 대신 위기 때의 convexity와 positive skew가 가치다(Man, CFM, Graham). (2) 실제 대형 기관의 거래비용은 학술 추정보다 한 자릿수 작다(AQR, 단 인내심 있는 알고리즘 집행 기준). (3) 충격은 거래량의 제곱근에 비례하고, 큰 주문은 쪼개져 체결 부호에 긴 기억을 남긴다(CFM).

### Cited Findings
- **The Need for Speed in Trend-Following Strategies** (Adi Mackic, Man Group/Man AHL, 2023-01-04). 1995~2022-08, 유동성 높은 선물·FX forward 50개를 쓴 double EWMA crossover 백테스트다(각 모형 연 변동성 10%). Sharpe는 느릴수록 높고, 비용을 빼면 빠른 모형이 장기적으로 "materially lower"다. skew는 빠른 모형이 더 높고, 가장 빠른 모형과 가장 느린 모형의 상관은 0.17이다. S&P 500 최악 5분위 구간("crisis alpha")은 빠를수록 좋았다. 표 수치는 이미지라 확인하지 못했다. **운용사 자료이고 hypothetical backtest다** — [Man](https://www.man.com/insights/need-for-speed-trend-following); 같은 주제의 [Paid to be paranoid: trending fast and slow](https://www.man.com/insights/paid-to-be-paranoid-trending-fast-and-slow)
- Graham Capital, "Speed of Trend"(2018-03, CME가 호스팅한 PDF, fetch 시간 초과로 본문 미확인)와 "Trend Following Primer"(2026-05). 검색 요약에 따르면 반응이 빠른 신호는 짧은 추세를 잡지만 whipsaw에 약하다. 속도는 이동평균 길이와 breakout lookback으로 정해진다 — [CME PDF](https://www.cmegroup.com/education/files/speed-of-trend-graham-research-march-2018.pdf), [Graham PDF](https://www.grahamcapital.com/wp-content/uploads/2026/05/Trend-Following-Primer_May-26.pdf)
- Lempérière et al.(2014, Bouchaud·Potters 공저): 짧은 추세는 "significantly withered", 긴 추세는 안정적이다. 저자 소속(CFM)은 페이지에서 확인하지 못했다 — [arXiv](https://arxiv.org/abs/1404.3274)
- **Trading Costs** (Frazzini, Israel, Moskowitz, AQR, 2018-08-23). 한 대형 운용사의 실제 체결 $1.7T(21개 선진 주식시장, 19년)로 추정한 결과, 실제 거래비용은 "an order of magnitude smaller than previous studies suggest"였다. 실제 비용에 맞춘 모형은 표본 밖에서 다른 문헌 모형보다 나았다 — [AQR](https://www.aqr.com/Insights/Research/Working-Paper/Trading-Costs)
- **Anomalous price impact and the critical nature of liquidity in financial markets** (Tóth, Lempérière, Deremble, de Lataillade, Kockelkoren, Bouchaud, *Physical Review X* 1, 2011). 현재가 주변 유동성이 V자로 얇아 square-root impact가 생긴다. 큰 metaorder는 잘게 쪼개야 흡수되고, 그 결과 주문 부호에 long memory가 생긴다 — [arXiv](https://arxiv.org/abs/1105.1694)
- **Big Data and AI Strategies: Machine Learning and Alternative Data Approach to Investing** (Marko Kolanovic & Rajesh T. Krishnamachari, J.P. Morgan Global Quantitative & Derivatives Strategy, 2017-05). ML 기법과 대체 데이터(감성, 카드 거래, 위성 이미지)를 개관한 보고서다(요약 기준 약 280쪽). 특정 단기 전략의 표본 밖 성과를 검증한 문서는 아니다 — [Cornell 강의자료 목록](https://coursematerials.johnson.cornell.edu/details/264c07a0). 인터넷의 PDF 사본([smallake.kr](https://www.smallake.kr/wp-content/uploads/2019/10/BIG-Data_AI-JPMmay2017.pdf))에는 특정 개인 전용 표기가 있어 공식 배포본이 아닐 수 있다.

### Inferences
- 4분 추세 추종(엔진의 momentum 계열)은 업계 근거로도 사전확률이 낮다. 빠른 추세의 가치는 적중률이 아니라 꼬리 국면의 convexity다. 적중률 기준 평가에서는 고변동성 국면에서만 조건부로 볼 만하다.
- AQR의 "비용이 작다"는 결과는 대형 기관의 인내심 있는 지정가 집행 이야기다. 한국 매도세 20bp와 개인 시장가 주문에는 그대로 옮길 수 없다.

### Gaps
- Two Sigma(Venn), Robeco(1차 보고서의 Blitz et al. 외), Research Affiliates, BlackRock/Invesco systematic의 공개 자료 가운데 분~일 단위 단기 기법을 다룬 것은 이번 한도 안에서 찾지 못했다.
- Goldman Sachs·Morgan Stanley 퀀트 노트는 공개 원문을 찾지 못했다.

## 5. 거래소·규제기관 연구: Cboe, SEC, KDI, 한국 규제

### Takeaway
규제기관 문서는 단기 기법을 합법과 불법으로 가른다. 대형 주문을 패턴으로 "예측"하는 order anticipation은 의무 위반이나 정보 유용이 없으면 SEC 기준으로 그 자체로 불법이 아니다. momentum ignition은 spoofing·허위 루머·stop 유발을 동반하면 시세조종이다. 2026년 한국의 핵심 규제 사건은 LETF 리밸런싱 분산 지침(금융위, 2026-07-28)이다.

### Cited Findings
- **Concept Release on Equity Market Structure** (SEC, Release No. 34-61358, 2010-01). PDF 본문에서 확인한 내용이다.
  - **Order anticipation**: 대형 매수(매도)자가 있음을 알아내고 먼저 사서(팔아서) 가격 움직임을 취한 뒤, 그 대형 주문에 되파는 전략이다. 대형 주문을 "free option"으로 삼기도 한다. 의무를 위반하거나 주문 정보를 유용하면 이미 금지된 front running이다. SEC가 이 문서에서 말하는 order anticipation은 그런 위반 없이 "sophisticated pattern recognition" 등으로 대형 주문을 알아내는 경우다.
  - **Momentum ignition**: 주문과 체결을 연달아 내고, 많은 주문을 빠르게 내고 취소해 다른 알고리즘을 "spoof"하거나 stop-loss를 유발해 급등락을 일으킨다. 미리 잡은 포지션을 그 움직임에 청산한다. SEC는 시세조종은 이미 금지돼 있고 허위 루머 유포는 불법이라고 적었다. 거래가 적은 종목에서 가장 해롭다고 봤다.
  - SEC는 구조적 취약점을 노린 유사 행위를 Section 10(b)·Rule 10b-5 위반으로 판단한 선례(Yoshikawa, 2006)도 각주로 인용한다 — [SEC PDF](https://www.sec.gov/files/rules/concept/2010/34-61358.pdf)
- Cboe 0DTE 자료(1절): 거래소가 고객·market maker 구분, 매수·매도, 개시·청산을 모두 보는 자료로 딜러 포지션을 계산했다. 외부의 가정 기반 GEX 추정보다 정확하다고 주장한다(이해당사자) — [Cboe](https://www.cboe.com/insights/posts/0-dt-es-decoded-positioning-trends-and-market-impact)
- KDI(박창균·임경묵 2003): KOSPI200 만기일 효과 — [KDI JEP](https://kdijep.org/v.25/2/137/Expiration+Day+Effects+in+Korean+Stock+Market+Wag+the+Dog+)
- 한국 LETF 규제: 금융위원회가 2026-07-28 운용사에 리밸런싱 시점을 앞당기고 분산하라고 요청했고 협회가 이틀 뒤 채택했다. 2026-07-29 경제부총리와 금융위원장이 국회에서 상품 도입을 사과했다. 홍콩 SFC는 2026-07-24 개정 circular로 LETF 운용사가 공시 상한 아래에서 배수를 매일 조정할 수 있게 했다(모두 Zhao 2026이 인용한 사실) — [arXiv](https://arxiv.org/abs/2608.03703)

### Inferences
- krx-quant는 주문을 내지 않으므로 momentum ignition·spoofing의 당사자가 될 수 없다. 다만 호가 기반 피처(queue imbalance, OFI)가 이런 행위로 오염될 수 있다는 1차 보고서의 지적(Citadel 사건)과 이어진다. 취소율이 비정상인 구간을 피처 신뢰도 하향 조건으로 쓸 수 있다.
- LETF 리밸런싱을 앞서 매매하는 것은 공개 정보만 쓰면 SEC 분류상 order anticipation에 가깝다. Zhao도 조작이나 담합 없이 성립하는 균형으로 모형화했다. 한국법상 판단은 확인하지 못했다.

### Gaps
- BIS Markets Committee(FX 실행 알고리즘), IOSCO, ESMA의 HFT 보고서, FCA occasional paper(1차 보고서의 Aquilina 외)는 이번에 직접 확인하지 못했다.
- 한국은행·자본시장연구원(KCMI)의 알고리즘 거래, 단일종목 LETF, weekly 옵션 보고서는 찾지 못했다.
- KOSPI200 선물·옵션의 투자자별 order imbalance가 단기 수익률(선물)이나 변동성(옵션)을 예측한다는 UNIST 세미나 요약, 장 초반 10분 옵션 imbalance가 남은 하루를 예측한다는 KAIST 학위논문은 검색 snippet으로만 봤다(**미확인**). 후자는 1차 보고서의 Kang, Kang, Lee(2022)와 같은 계열로 보인다 — [UNIST](https://ie.unist.ac.kr/eng/?p=655)(리다이렉트로 본문 미확인), [KAIST](https://koasas.kaist.ac.kr/handle/10203/294444)

## 6. 집행 알고리즘의 발자국: VWAP/TWAP/POV/IS, Almgren–Chriss, 탐지

### Takeaway
기관 집행 알고리즘은 **거래량은 예측 가능하게**(장중 거래량 곡선) 만들지만, 가격 방향은 약하게만 남긴다. 쪼갠 주문은 체결 부호에 long memory를 남긴다. HFT는 처음에는 대형 주문의 반대편에 서다가, 정보가 있는 주문이면 같은 방향으로 돌아선다(back-running). 그래서 "지속되는 한쪽 흐름 = 정보 있는 parent order"라는 조건부 지속 신호가 가능하다. 다만 체결 부호를 예측해도 가격 예측으로 이어지지 않는다는 1차 보고서의 결과(Lillo & Farmer)와 함께 읽어야 한다.

### Cited Findings
- **High-Frequency Trading around Large Institutional Orders** (van Kervel & Menkveld, *JF* 74(3), 2019). HFT는 child order로 나뉜 대형 기관 주문에 처음에는 반대로 거래한다(lean against the wind). 가장 정보가 많은 주문에서는 결국 같은 방향으로 거래한다. 기관은 더 세게 거래해 얻는 이익과 HFT에게 탐지돼 "preyed on"될 위험을 저울질한다 — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v74y2019i3p1091-1137.html)
- **Improving VWAP strategies: A dynamic volume approach** (Bialkowski, Darolles, Le Fol, *JBF* 32(9), 2008). 거래량을 시장 전체 성분과 종목 고유 성분(ARMA/SETAR)으로 나눴다. 장중에 동적으로 조정하면 VWAP 추적이 나아진다. 표본은 2004-09 CAC 40이다 — [IDEAS](https://ideas.repec.org/a/eee/jbfina/v32y2008i9p1709-1722.html)
- Almgren & Chriss, "Optimal Execution of Portfolio Transactions"(*Journal of Risk* 3(2), 2000/01): 기대 비용과 분산의 trade-off로 집행 경로의 efficient frontier를 정의한 표준 모형이다(위험회피가 클수록 앞쪽에 몰아 집행). 원문 PDF는 403/404로 이번에 확인하지 못했고, 설명은 일반적 내용이다 — [DOI](https://doi.org/10.21314/JOR.2001.041)
- square-root impact와 metaorder 분할(CFM, Tóth et al. 2011), 실제 비용(AQR, Frazzini et al. 2018)은 4절 — [arXiv](https://arxiv.org/abs/1105.1694), [AQR](https://www.aqr.com/Insights/Research/Working-Paper/Trading-Costs)
- SEC의 order anticipation 정의("pattern recognition"으로 대형 주문 탐지)는 5절 — [SEC PDF](https://www.sec.gov/files/rules/concept/2010/34-61358.pdf)

### Inferences
- VWAP·POV 알고리즘은 U자형 거래량 곡선을 따라간다. 그래서 거래량과 변동폭(엔진의 HAR range)은 예측하기 쉽고, 방향은 어렵다. 장중 거래량 예측은 range·barrier 종류의 입력으로 쓸 만하다.
- 엔진의 회원사별(외국계 창구) 실시간 순매수가 한 방향으로 오래 이어지면 parent order 진행 중의 대리 변수가 될 수 있다. van Kervel & Menkveld에 따르면 그 방향 지속은 주문이 "정보가 있을 때"만 기대할 수 있다. 정보 없는 리밸런싱성 흐름이면 끝난 뒤 되돌림을 예상해야 한다(Bogousslavsky & Muravyev, Zhao).

### Gaps
- KRX 개별 종목에서 집행 알고리즘의 탐지 가능성이나 회원사 흐름의 지속성을 실증한 공개 연구는 찾지 못했다.
- Almgren–Chriss 원문과 metaorder 완료 뒤 되돌림 비율 같은 수치는 이번에 확인하지 못했다.

## krx-quant 적용 메모

엔진 데이터(KIS 실시간 체결·호가, 프로그램·회원사·투자자 수급, KOSPI200 선물, 달러선물, ATM KOSPI200 옵션 체결과 IV, 미국 주간 NVDA/MU/AMD, 1분봉 1년, 일별 수급 10년, 일별 매크로)로 무엇을 잴 수 있는지 정리했다. 등급은 "지금 데이터로 측정 가능 / 데이터 추가 필요 / 측정 불가"다.

| 순서 | 메커니즘 | 측정 가능성 | 무엇을 재나 | 근거 |
|---|---|---|---|---|
| 1 | **단일종목 LETF 마감 리밸런싱** (삼성전자·SK하이닉스) | 데이터 추가 필요: 16개 LETF(와 KOSPI200 레버리지·인버스)의 일별 순자산·배수. 그 밖에는 있음 | 예상 리밸런싱 d = Σ A_k(L_k²−L_k)·r(전일 종가→t)를 1분마다 계산한다. 최근 동시호가 체결대금으로 나눈 d/W를 피처로 쓴다. 예측 대상은 (a) 15:20 전 같은 방향 지속, (b) 동시호가 가격 변화, (c) 다음날 시가·종가 반전. 표본은 5/27 이전, 5/27~7/30, 7/30 이후로 나눈다 | [Zhao 2026](https://arxiv.org/abs/2608.03703), [Cheng & Madhavan](https://joim.com/dynamics-leveraged-inverse-exchange-traded-funds) |
| 2 | **동시호가 예상체결가와 마감 이탈 되돌림** | 측정 가능할 것으로 보임(KIS 예상체결가 제공 여부 확인 필요) | 15:20~15:30 예상가와 15:20 가격의 차이로 종가를 예측하고, 종가 이탈의 다음날 시가 되돌림을 본다. VI 연장(15:32) 날은 따로 표시한다 | [Bogousslavsky & Muravyev 2023](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html), [Zhao 2026](https://arxiv.org/abs/2608.03703) |
| 3 | **만기일 효과·지수 pinning** (월물 둘째 목요일, weekly 목·월) | 측정 가능(1분봉, KOSPI200 선물, 프로그램 수급) | 만기일과 평일에 KOSPI200 종가와 가장 가까운 행사가의 거리 분포를 비교한다(Golez & Jackwerth형). 만기일 마감 차익 프로그램 순매매와 다음날 반전도 본다. 1분봉 1년이면 월물 약 12회, weekly 약 100회로 표본이 작다. 행사가 간격은 KRX 자료로 확인해야 한다 | [Golez & Jackwerth 2012](https://ideas.repec.org/a/eee/jfinec/v106y2012i3p566-585.html), [박창균·임경묵 2003](https://kdijep.org/v.25/2/137/Expiration+Day+Effects+in+Korean+Stock+Market+Wag+the+Dog+) |
| 4 | **KOSPI200 옵션 dealer gamma** | 지금 데이터로는 측정 불가. ATM 체결만 있고 행사가별 미결제약정과 딜러 부호가 없다 | 대용치로 ATM IV 수준·변화, ATM 체결의 aggressor 기준 순매수 압력을 국면(변동성) 피처로 쓴다. 방향 신호로 쓰지 않는다. 진짜 GEX에는 행사가별 OI와 투자자별 포지션(KRX 공개 여부 확인)이 필요하고, 그래도 부호 가정 위험이 남는다 | [Barbon & Buraschi](https://abarbon.com/papers/gamma-fragility), [Cboe 2025](https://www.cboe.com/insights/posts/0-dt-es-decoded-positioning-trends-and-market-impact), [Ryu et al. 2022](https://scholar.xjtlu.edu.cn/en/publications/investors-net-buying-pressure-and-implied-volatility-dynamics/) |
| 5 | **프로그램 매매·회원사 흐름의 지속** (집행 알고리즘 발자국) | 측정 가능(실시간 프로그램·회원사 수급) | 외국계 창구 순매수가 n분 연속 한 방향일 때 이후 4~30분 방향을 본다. 정보 있는 흐름(지속)과 리밸런싱 흐름(되돌림)을 시각(마감 근처)과 LETF d로 구분한다 | [van Kervel & Menkveld 2019](https://ideas.repec.org/a/bla/jfinan/v74y2019i3p1091-1137.html), [Tóth et al. 2011](https://arxiv.org/abs/1105.1694) |
| 6 | **장중 거래량 곡선** | 측정 가능(1분봉) | 시장 성분과 종목 성분으로 거래량을 예측해 range·barrier 종류의 입력으로 쓴다(방향용 아님) | [Bialkowski et al. 2008](https://ideas.repec.org/a/eee/jbfina/v32y2008i9p1709-1722.html) |
| 7 | **월말·분기말·연기금 리밸런싱** | 측정 가능(일별 투자자 수급 10년, 연기금 구분 확인) | 월중 주식이 크게 오른(내린) 달의 월말 다음날 수익률과 분기말 마지막 10분의 가격 급등·다음날 반전을 본다 | [Harvey et al. 2025](https://www.nber.org/papers/w33554), [Carhart et al. 2002](https://ideas.repec.org/a/bla/jfinan/v57y2002i2p661-693.html) |
| 8 | **pre-FOMC·미국 매크로 발표** | 측정 가능(일별 10년, 약 80회) | FOMC 발표 직전 한국 정규장 수익률과 미국 CPI·고용 발표 다음날 갭 크기를 본다. 방향보다 변동성 국면 플래그로 쓴다. 사전확률은 낮다(Nikkei 무효과, 2015년 이후 소멸) | [Lucca & Moench](https://www.newyorkfed.org/research/staff_reports/sr512.html), [Kurov et al. 2021](https://ideas.repec.org/a/eee/finlet/v40y2021ics1544612320315956.html) |
| — | 짧은 추세 추종, 공개 put-call ratio, vanna/charm, max pain | 비추천 | 짧은 추세는 약해졌고, PCR 예측력은 비공개 개시 매수 자료에서 나왔으며, vanna/charm은 독립 검증이 없다 | [Lempérière et al. 2014](https://arxiv.org/abs/1404.3274), [Pan & Poteshman 2006](https://ideas.repec.org/a/oup/rfinst/v19y2006i3p871-908.html) |

추가 메모:
- **2026년 국면 단절**: KOSPI 평균 일 변동은 2025년 1.0%에서 5/27 이후 석 달 4.1%가 됐다. 같은 기간 서킷브레이커 7번, 사이드카 31번, LETF 분산 지침(7/28~30)이 있었다([Zhao 2026](https://arxiv.org/abs/2608.03703)). 엔진의 1분봉 1년 학습 창은 이 단절을 가로지른다. 적중률은 국면별로 나눠 보고하는 편이 맞다.
- **마감 흐름은 4분 신호의 오염원이자 새 예측 종류의 재료다.** 14:30 이후의 4분 방향 적중률은 LETF d와 동시호가 예상가를 조건으로 넣기 전과 뒤를 비교해야 한다. 다음날 반전(75%)은 1차 보고서가 제안한 `gap`·`open_to_close` 종류에 가장 직접 들어간다.
- **불법 경계**: 엔진은 신호만 내므로 해당하지 않는다. 다만 사용자가 신호로 직접 매매할 때 LETF 리밸런싱 선행 매매나 호가 조작성 행위와 섞이지 않게 한다. momentum ignition·spoofing은 SEC 기준으로도, 1차 보고서의 한국 Citadel 사건 기준으로도 시세조종 영역이다.
