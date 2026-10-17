# 스윙 트레이딩(보유 수일~수주) 퀀트 전략: 공개된 근거 정리

기준일: 2026-10-17. 연구 정리이며 투자 조언이 아니다.
표기 규칙:
- **(미확인)**: 이번 조사에서 초록이나 원문으로 확인하지 못하고 기존 지식에 기댄 수치나 서술.
- **(오래된 결과)**: 표본이 2010년 이전에 끝난 결과.
- 서지(제목, 저자, 학술지, 연도)는 Crossref, OpenAlex, Semantic Scholar 메타데이터로 확인했다. 초록은 OpenAlex, Semantic Scholar, 출판사 페이지, 검색 요약에서 읽었다.
- 조사 도중 웹 검색 한도가 다 찼다. 그래서 일부 세부 수치(HOP 2017의 Sharpe, MOP 2012의 Sharpe 등)는 확인하지 못했다. 각 절의 Gaps에 적었다.

---

## 1. Short-term reversal (단기 반전): Lehmann, Jegadeesh, Lo-MacKinlay, Nagel, industry/residual reversal

### Takeaway
개별 주식의 1주~1개월 수익률은 다음 기간에 되돌아간다(reversal). 다만 이 수익은 대체로 "유동성 공급의 대가"다. 그래서 변동성(VIX)이 높을 때 크다. 단순한 cross-sectional 버전은 2002년 이후 비용을 빼면 거의 사라졌다. 펀더멘털 뉴스와 공통 요인을 걷어 낸 residual 버전이 더 강하다. 거래회전율이 높은 대형주에서는 오히려 short-term momentum이 나타난다는 반론이 있다(Medhat & Schmeling 2022).

### Cited Findings
- **규칙**: 지난 1주(또는 1개월) 수익률 하위 종목을 사고 상위 종목을 판다. 1주~1개월 보유한다.
- Lehmann (1990, QJE) "Fads, Martingales, and Market Efficiency": 한 주의 승자와 패자가 다음 주에 크게 되돌아간다. 이 이익은 bid-ask spread와 "그럴듯한 거래비용"을 보정해도 남는다. 저자는 이를 큰 가격 변동 주변에서 유동성 시장이 비효율적이라는 신호로 해석했다. 미국 주식 대상. (오래된 결과) — [DOI](https://doi.org/10.2307/2937816)
- Jegadeesh (1990, JF) "Evidence of Predictable Behavior of Security Returns": 월간 수익률의 1차 자기상관은 유의한 음수다. 12개월 시차의 자기상관은 양수다. 1934–1987년 극단 decile 사이의 abnormal return 차이는 월 2.49%다. 비용 차감 전. (오래된 결과) — [DOI](https://doi.org/10.1111/j.1540-6261.1990.tb05110.x)
- Lo & MacKinlay (1990, RFS) "When Are Contrarian Profits Due to Stock Market Overreaction?": 주간 contrarian 이익의 상당 부분은 과잉반응이 아니라 cross-autocorrelation에서 나온다. 대형주가 소형주를 선행(lead)한다. (오래된 결과) — [DOI](https://doi.org/10.1093/rfs/3.2.175)
- Nagel (2012, RFS) "Evaporating Liquidity": 단기 반전 수익은 유동성 공급 수익의 proxy로 볼 수 있다. VIX로 강하게 예측된다. 위기 때 기대수익과 조건부 Sharpe가 급등한다. — [DOI](https://doi.org/10.1093/rfs/hhs066)
- Da, Liu & Schaumburg (2014, Management Science) "A Closer Look at the Short-Term Return Reversal": 애널리스트 추정치 수정으로 cash-flow 뉴스를 걷어 낸다. 그렇게 남은 비펀더멘털 수익률로 만든 enhanced reversal은 위험조정 수익이 표준 reversal의 4배다. 롱 쪽 반전은 유동성 충격(fire sale)에서, 숏 쪽 반전은 투자 심리에서 나온다. — [DOI](https://doi.org/10.1287/mnsc.2013.1766)
- Blitz, Huij, Lansdorp & Verbeek (2013, Journal of Financial Markets) "Short-Term Residual Reversal": Fama-French 요인 잔차로 만든 반전. 총수익 반전보다 위험조정 성과가 좋다는 결과로 알려져 있다. (미확인: 초록을 얻지 못함) — [DOI](https://doi.org/10.1016/j.finmar.2012.10.005)
- Do & Faff (2012, Journal of Financial Research): 업종 대비(industry-relative) 단기 반전과 pairs trading은 둘 다 2002년 이후 거래비용을 빼면 대체로 수익이 없다(미국 1963–2009). — [DOI](https://doi.org/10.1111/j.1475-6803.2012.01317.x)
- Medhat & Schmeling (2022, RFS) "Short-term Momentum": 전월 수익률과 거래회전율(turnover)로 이중 정렬한 결과다.
  - 회전율이 낮은 종목은 반전한다.
  - 회전율이 높은 종목은 short-term momentum을 보인다.
  - 이 momentum은 비용 후에도 남고, 가장 크고 유동적이며 애널리스트 커버리지가 많은 종목에서 가장 강하다. 미국과 해외 모두 확인했다. — [DOI](https://doi.org/10.1093/rfs/hhab055)
- Dai, Medhat, Novy-Marx & Rizova (2024, FAJ) "Reversals and the Returns to Liquidity Provision":
  - 변동성이 높으면 반전이 빠르고 초기에 강하다.
  - 회전율이 낮으면 반전이 오래가고 결국 더 강하다.
  - 미국 밖에서도 성립한다. — [DOI](https://doi.org/10.1080/0015198X.2023.2292534)
- Zaremba, Long & Karathanasopoulos (2019, JIFMIM) "Short-term momentum (almost) everywhere": 지수, 채권, 상품, 통화 같은 자산군 단위에서는 단기 반전이 아니라 short-term momentum이 나타난다. 직전 1개월 수익률이 다음 달 수익률을 양(+)으로 예측한다. — [DOI](https://doi.org/10.1016/j.intfin.2019.101140)

### Inferences
- 삼성전자와 SK하이닉스는 회전율이 매우 높은 대형주다. Medhat-Schmeling 결과대로라면 "직전 며칠 하락하면 사라"는 단순 반전보다, 회전율이 높은 날에는 short-term momentum 쪽이 맞을 수 있다. 종목별로 시계열 검증이 필요하다.
- 반전 신호를 쓴다면 시장(KOSPI200 선물)과 반도체 요인(SOX/NVDA/MU 야간 수익률)을 걷어 낸 residual 기준이 문헌상 더 낫다. 변동성(VKOSPI)이 높을 때 신호를 키우는 조건화가 Nagel과 Dai et al.에 부합한다.

### Gaps
- Blitz et al.(2013)의 정확한 수치(Sharpe, 표본)는 확인하지 못했다.
- 한국 시장에서 주간(1주) 단기 반전의 비용 후 성과를 다룬 최신 논문은 찾지 못했다. 월간 결과는 7절에 있다.

---

## 2. Momentum과 time-series momentum(TSMOM): J-T, MOP, HOP, 52-week high, residual momentum, momentum crash

### Takeaway
3–12개월 cross-sectional momentum과 자산별 TSMOM(추세추종)은 수십 년, 여러 자산군에서 확인됐다. 다만 crash 위험이 크고, 개별 자산 수준의 예측력은 약하다는 반론이 있다(Huang et al. 2020). 스윙(수일~수주) 보유에는 "직전 1개월을 빼고 12개월로 계산한 신호"보다는 52-week high 근접도나 residual momentum 같은 변형이 더 쓸모 있다.

### Cited Findings
- **규칙(J-T)**: 지난 3–12개월 승자를 사고 패자를 판다. 3–12개월 보유한다.
- Jegadeesh & Titman (1993, JF) "Returns to Buying Winners and Selling Losers":
  - 3–12개월 보유에서 유의한 양(+)의 수익을 낸다.
  - 첫 해 초과수익의 일부는 이후 2년 동안 사라진다.
  - 표본은 미국 1965–1989년이다. (표본 기간은 미확인, 오래된 결과) — [DOI](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x)
- **규칙(TSMOM)**: 자산별로 지난 12개월 초과수익의 부호를 따라 롱이나 숏을 잡는다. 1개월 보유하고, 보통 변동성으로 스케일링한다.
- Moskowitz, Ooi & Pedersen (2012, JFE) "Time series momentum":
  - 유동적인 선물·선도 58종(주가지수, 통화, 상품, 채권) 모두에서 TSMOM이 유의하다.
  - 수익률은 1–12개월 지속되고, 더 긴 시계에서는 일부 반전한다.
  - 분산 포트폴리오는 표준 요인 노출이 작고 극단 장세에서 가장 잘 된다. — [DOI](https://doi.org/10.1016/j.jfineco.2011.11.003)
- Hurst, Ooi & Pedersen (2017, JPM) "A Century of Evidence on Trend-Following Investing": 1880년까지 거슬러 백테스트했다. 이후 110년 동안 꾸준히 수익을 냈다. 비용과 수수료를 반영한 세부 수치는 미확인. — [DOI](https://doi.org/10.3905/jpm.2017.44.1.015), [AQR 요약](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)
- Huang, Li, Wang & Zhou (2020, JFE) "Time series momentum: Is it there?" (반론):
  - 자산별 시계열 회귀에서는 TSMOM 예측력이 표본 내외 모두 거의 없다.
  - pooled 회귀의 t값은 bootstrap 임계값보다 작다.
  - TSMOM 전략의 수익은 "과거 표본평균 부호" 전략과 거의 같다. — [DOI](https://doi.org/10.1016/j.jfineco.2019.08.004)
- Kim, Tse & Wald (2016, JFM) "Time series momentum and volatility scaling": TSMOM 성과의 상당 부분은 변동성 스케일링에서 나오고, 스케일링을 빼면 buy-and-hold와 비슷하다. (미확인: 초록을 얻지 못함) — [DOI](https://doi.org/10.1016/j.finmar.2016.05.003)
- **규칙(52주 고가)**: 현재가 ÷ 52주 최고가가 큰 종목을 사고 작은 종목을 판다.
- George & Hwang (2004, JF) "The 52-Week High and Momentum Investing":
  - 52주 고가 근접도가 과거수익률(개별, 업종) 기반 momentum 이익의 큰 부분을 설명하고, 예측력에서 앞선다.
  - 이 신호의 수익은 장기에 반전하지 않는다. — [DOI](https://doi.org/10.1111/j.1540-6261.2004.00695.x)
- **규칙(residual momentum)**: 36개월 Fama-French 3요인 회귀 잔차를 12개월 표준편차로 나눈 값으로 순위를 매긴다(직전 1개월 제외).
- Blitz, Huij & Martens (2011, JEF) "Residual momentum": 1926–2009년, gross 기준이다. 연 Sharpe는 residual 0.90, total-return momentum 0.45다. 연 변동성은 12.5%와 22.7%다. 즉 개선은 주로 변동성 감소에서 나온다. 비용 차감 전. (수치 출처: CXO 요약) — [DOI](https://doi.org/10.1016/j.jempfin.2011.01.003), [CXO 요약](https://www.cxoadvisory.com/momentum-investing/stripping-risks-from-a-stock-momentum-strategy/)
- Daniel & Moskowitz (2016, JFE) "Momentum crashes":
  - momentum crash는 시장 급락 뒤 변동성이 높은 "panic state"에서, 시장 반등과 동시에 일어나며 일부 예측할 수 있다.
  - 평균과 분산 예측에 기반한 동적 momentum은 정적 전략 대비 alpha와 Sharpe를 약 2배로 높인다. 해외 주식과 다른 자산군에서도 확인됐다. — [DOI](https://doi.org/10.1016/j.jfineco.2015.12.002)
- Barroso & Santa-Clara (2015, JFE) "Momentum has its moments": momentum을 자기 실현변동성으로 스케일링(risk-managed momentum)하면 crash가 크게 줄고 Sharpe가 오른다. (미확인: 초록을 얻지 못함) — [DOI](https://doi.org/10.1016/j.jfineco.2014.11.010)
- Novy-Marx (2012, JFE) "Is momentum really momentum?": momentum 이익은 주로 12–7개월 전 수익에서 나온다는 주장이다. (미확인) — [DOI](https://doi.org/10.1016/j.jfineco.2011.05.003)
- Ehsani & Linnainmaa (2022, JF) "Factor Momentum and the Momentum Factor": factor 수익에도 momentum이 있다. 이 논문이 존재한다는 것만 확인했다. — [DOI](https://doi.org/10.1111/jofi.13131)

### Inferences
- 종목이 셋뿐이라 cross-sectional J-T는 의미가 약하다. 시계열(TSMOM이나 추세) 형태로 바꿔야 한다. 다만 Huang et al.은 단일 자산 TSMOM의 통계적 근거가 약하다고 지적한다. 그래서 단독 신호보다는 국면(regime) 변수로 쓰는 쪽이 안전하다.
- 52주 고가 근접도와 residual(시장·반도체 요인 제거) 추세는 종목 셋에도 그대로 계산되는 시계열 feature다.
- Daniel-Moskowitz의 panic state(시장 급락 뒤 고변동) 조건은 "추세 신호를 끄거나 줄이는" 규칙으로 쓸 수 있다.

### Gaps
- MOP 2012와 HOP 2017의 Sharpe·연수익 수치, 수수료 가정은 이번에 확인하지 못했다(검색 한도 소진).
- J-T 1993의 표본 기간과 월 수익 수치는 원문으로 확인하지 못했다.

---

## 3. Statistical arbitrage와 pairs trading: GGR, Avellaneda-Lee, Do-Faff 감쇠, cointegration/copula, Krauss survey

### Takeaway
pairs와 stat-arb는 1960–2000년대 초까지는 비용 후에도 수익이 났다. 2002년 무렵부터 수익이 꾸준히 줄었다. 위기나 고변동 국면에서만 강하게 살아나는 패턴이 반복된다. 이익의 상당 부분은 결국 단기 반전과 업종 momentum으로 설명된다(Chen et al. 2019).

### Cited Findings
- **규칙(distance, GGR)**: 12개월 동안 정규화한 가격 경로의 거리가 가장 작은 쌍을 고른다. 6개월 동안 스프레드가 2σ 넘게 벌어지면 진입하고, 교차하면 청산한다.
- Gatev, Goetzmann & Rouwenhorst (2006, RFS) "Pairs Trading": 미국 1962–2002년 일별 데이터. self-financing 포트폴리오가 연 최대 11%의 초과수익을 낸다. 보수적인 거래비용 추정치를 대체로 넘는다. 기존 반전 이익과는 다른 효과로 본다. (오래된 결과) — [DOI](https://doi.org/10.1093/rfs/hhj020)
- Do & Faff (2010, FAJ) "Does Simple Pairs Trading Still Work?": 수익성이 계속 줄어드는 추세를 확인했다. 다만 장기 혼란기(2008 GFC 포함)에는 강하다. — [DOI](https://doi.org/10.2469/faj.v66.n4.1)
- Do & Faff (2012, JFR): 수수료, market impact, 공매도 비용을 반영하면 위험조정 수익은 월 약 30bp다(세분화한 업종 안에서 잘 맞춘 쌍 기준). 2002년 이후에는 대체로 수익이 없다. — [DOI](https://doi.org/10.1111/j.1475-6803.2012.01317.x)
- Rad, Low & Faff (2016, Quantitative Finance): 미국 1962–2014년에 시변 거래비용을 반영했다.
  - 월 초과수익은 비용 전 distance 91bp, cointegration 85bp, copula 43bp다.
  - 비용 후에는 38bp, 33bp, 5bp다.
  - 2009년 이후 distance와 cointegration은 거래 기회가 크게 줄었다.
  - cointegration은 격변기에 가장 좋다. — [DOI](https://doi.org/10.1080/14697688.2016.1164337)
- **규칙(Avellaneda-Lee)**: 종목 수익률을 PCA 요인이나 섹터 ETF에 회귀한다. 잔차를 OU(mean-reverting) 과정으로 모델링하고, s-score가 임계값을 넘으면 역방향으로 진입한다. 보유는 수일 단위다.
- Avellaneda & Lee (2010, Quantitative Finance):
  - 비용 후 PCA 전략의 Sharpe는 1997–2007년 1.44, 2003–2007년 0.9다.
  - ETF 전략은 1.1이고, 2002년 이후 비슷하게 떨어졌다.
  - 거래량 정보를 넣은 ETF 전략은 2003–2007년 1.51이다. — [DOI](https://doi.org/10.1080/14697680903124632), [NYU working paper](https://math.nyu.edu/~avellane/AvellanedaLeeStatArb071008.pdf)
- Chen, Chen, Chen & Li (2019, Management Science) "Empirical Investigation of an Equity Pairs Trading Strategy": pairs 이익은 대부분 단기 반전과 1개월 업종 momentum으로 설명된다. — [DOI](https://doi.org/10.1287/mnsc.2017.2825)
- Krauss (2017, Journal of Economic Surveys): pairs trading 문헌을 다섯 접근으로 정리했다. distance, cointegration, time-series(최적 매매 규칙), stochastic control, 기타. 100편 넘는 문헌을 다룬다. — [DOI](https://doi.org/10.1111/joes.12153)
- Krauss, Do & Huck (2017, EJOR): S&P 500에서 DNN, GBT, RF로 일간 stat-arb를 했다. 수익이 후기로 갈수록 줄었다는 결과로 알려져 있다. (미확인: 수치 확인 못 함) — [DOI](https://doi.org/10.1016/j.ejor.2016.10.031)

### Inferences
- 삼성전자, SK하이닉스, 삼성전기는 공통 요인(메모리와 반도체 사이클, 외국인 수급)이 강한 쌍 후보다. 다만 한 쌍이나 세 쌍으로는 GGR식 분산 효과가 없다. 그래서 "어느 종목이 N일 동안 더 오를까"라는 상대수익 방향 예측으로 바꾸는 편이 현실적이다.
- 개인 계좌에서 숏 쪽은 인버스 상품으로만 표현할 수 있다. 그래서 스프레드 자체를 매매하기보다 "상대 강약" 신호로 롱 종목을 고르는 용도가 맞다.

### Gaps
- 한국 대형주 pairs trading의 비용 후 성과를 다룬 동료심사 논문은 찾지 못했다.
- copula pairs의 2015년 이후 성과는 확인하지 못했다.

---

## 4. Event-driven: PEAD, 지수 편입·리밸런싱, 옵션 만기

### Takeaway
PEAD는 대형주에서 2006년 무렵 사실상 사라졌다(Martineau 2022). 남은 drift는 주로 microcap에 있다. 미국 S&P 500 편입 효과는 1990년대 7.4%에서 최근 10년 1% 미만으로 줄었다. 한국 KOSPI200 변경 효과와 만기일 효과는 있다는 연구가 있지만 대부분 오래된 결과다.

### Cited Findings
- **규칙(PEAD)**: 실적 서프라이즈(SUE) 상위 종목을 사고 하위 종목을 판다. 발표 후 약 60거래일 보유한다.
- Bernard & Thomas (1989, JAR) "Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?" — [DOI](https://doi.org/10.2307/2491062). 후속 연구인 Bernard & Thomas (1990, JAE) — [DOI](https://doi.org/10.1016/0165-4101(90)90008-R). PEAD의 고전이다. (오래된 결과)
- Chordia, Goyal, Sadka, Sadka & Shivakumar (2009, FAJ) "Liquidity and the Post-Earnings-Announcement Drift":
  - 롱숏 PEAD의 value-weighted 월 수익은 유동성 최상위 종목에서 0.04%, 최하위 종목에서 2.43%다.
  - 거래비용이 장부상 이익의 70–100%를 잡아먹는다. — [DOI](https://doi.org/10.2469/faj.v65.n4.3)
- Martineau (2022, Critical Finance Review) "Rest in Peace Post-Earnings Announcement Drift": 대형주에서는 2006년 이후 PEAD가 없다. microcap에서도 최근에야 사라졌다. 가격이 발표 당일에 서프라이즈를 다 흡수한다. — [RePEc](https://ideas.repec.org/a/now/jnlcfr/104.00000122.html), [PDF](https://cfr.ivo-welch.info/published/papers/martineau2021rest.pdf)
- 2025년 논쟁:
  - Hirshleifer, Peng & Wang (2025, RFS)과 Dickerson, Julliard & Mueller(JFE 게재 예정)는 drift가 살아 있다고 본다.
  - Subrahmanyam(2025, SSRN WP)은 2001.2–2024.12년 재현에서 t값이 전체 표본 2.18, microcap 제외 1.43이라고 보고했다. 그는 차이가 연구 설계(microcap 포함 여부)에서 온다고 본다.
  - 출처는 UCLA Anderson Review 요약이다. 개별 논문 URL은 미확인. — [UCLA Anderson Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again/)
- **규칙(지수 편입)**: 편입 발표 후 매수하고 실제 편입일에 판다. 또는 편입일 전후의 반전을 노린다.
- Shleifer (1986, JF) "Do Demand Curves for Stocks Slope Down?": S&P 500 편입 효과의 고전이다. (오래된 결과) — [DOI](https://doi.org/10.1111/j.1540-6261.1986.tb04518.x)
- Greenwood & Sammon (2025, JF) "The Disappearing Index Effect": S&P 500 편입 abnormal return은 1990년대 평균 7.4%에서 최근 10년 1% 미만으로 줄었다. 제외 종목은 2010–2020년 평균 0.1%다. 다른 지수에서도 비슷하게 줄었다. — [DOI](https://doi.org/10.1111/jofi.13410)
- 한국 KOSPI200: Yun & Kim (2010, International Review of Financial Analysis)은 변경 종목에 영구적 가격 효과와 부분 반전이 있다고 보고했다. 거래량은 늘어난 채 유지된다. 편입 종목은 이전 실적과 이익이 좋았다(선정 방식에 정보가 있다). 서지는 검색 요약 기준이다. — [RePEc](https://ideas.repec.org/a/eee/finana/v19y2010i4p258-269.html)
- KOSPI200 편입 종목의 초과수익과 이상 거래량, 외국인 지분 증가 여부의 영향을 다룬 KAIST 석사논문(2005)이 있다. — [KAIST](https://koasas.kaist.ac.kr/handle/10203/52262)
- **규칙(옵션 만기)**: 만기일에 행사가 근처로 끌려가는 현상(pinning)이나 만기 프로그램 매매로 생긴 종가 왜곡의 반전을 노린다.
- Ni, Pearson & Poteshman (2005, JFE) "Stock price clustering on option expiration dates": 옵션이 있는 종목의 만기일 종가는 행사가 근처로 몰린다. 만기 하루가 수익률을 최소 16.5bp 바꾼다. 원인으로 마켓메이커 헤지와 일부 조작을 든다. (오래된 결과) — [DOI](https://doi.org/10.1016/j.jfineco.2004.08.005)
- Park & Lim (2003, KDI Journal of Economic Policy) "Expiration Day Effects in Korean Stock Market: Wag the Dog?": KOSPI200 파생상품 만기일에 현물시장의 가격, 변동성, 거래량 효과가 강하다. 마지막 10분 단일가 정산 구조와 관련된다. (오래된 결과) — [DOI](https://doi.org/10.23895/kdijep.2003.25.1.137)

### Inferences
- 삼성전자는 PEAD 문헌상 "drift가 사라진 대형주"에 속한다. SK하이닉스와 삼성전기도 마찬가지다. 따라서 PEAD 단독 전략의 기대는 낮다. 다만 삼성전자 잠정실적과 Micron 실적(업계 read-through) 같은 이벤트는 변동성과 방향의 국면 변수로 쓸 가치가 있다.
- 세 종목은 이미 KOSPI200과 MSCI 구성 종목이라 편입·제외 이벤트는 없다. 지수 정기 변경(비중 조정)이나 유동주식수 변경 같은 리밸런싱일 수급은 이벤트 flag로 넣을 수 있다.
- 옵션 만기(매월 둘째 목요일)는 스윙 신호라기보다 "그날 종가는 노이즈"라는 플래그로 쓰는 쪽이 맞다.

### Gaps
- 한국 PEAD(특히 대형주와 잠정실적 공시)의 최근 연구는 이번 조사에서 확인하지 못했다.
- Chay·류혁선(2006, 증권학회지) "Expiration-Day Effects of the KOSPI 200 Futures and Options"는 OpenAlex에서 존재만 확인했다. 작동하는 URL은 얻지 못했다.
- 한국 지수 변경 효과의 2015년 이후 감쇠 여부는 미확인이다.

---

## 5. 대중적 규칙 기반 스윙 시스템: RSI(2)/Connors, Bollinger reversion, Turtle/Donchian breakout

### Takeaway
RSI(2)와 Connors 계열은 동료심사를 거친 독립적인 표본 외 검증을 찾지 못했다. 근거는 저자 본인의 책과 실무 블로그뿐이다. Bollinger Bands 추종은 비용 후 buy-and-hold를 이기지 못했다. 역방향(contrarian) 사용만 나아진다. 단순 기술적 규칙 전반은 data-snooping을 보정하고 비용을 넣으면 가치가 거의 사라진다(Bajgrowicz & Scaillet 2012). Donchian과 Turtle 같은 breakout의 학술적 대응물은 분산 선물 포트폴리오의 TSMOM(2절)이다. 개별 주식 단독의 근거는 약하다.

### Cited Findings
- **규칙(RSI(2), Connors)**: 종가가 200일 이동평균 위(장기 상승 추세)이고 RSI(2)가 5–10 미만이면 매수한다. 종가가 5일 이동평균 위로 올라오면 청산한다. 출처는 Connors & Alvarez의 책 "Short Term Trading Strategies That Work"(2008)다. 동료심사 논문에서 독립적으로 표본 외 검증한 것은 찾지 못했다. 2026년 3월 한 유료 뉴스레터가 "17년 넘게 표본 외에서 살아남았다"고 주장하지만 방법론은 확인할 수 없다. — [Substack(실무 주장, 미검증)](https://algotr.substack.com/p/this-simple-mean-reversion-strategy)
- **규칙(Bollinger)**: 20일 ±2σ 밴드를 이탈하면 추세 방향으로(또는 역방향으로) 진입한다.
- Lento, Gradojevic & Wright (2007, Applied Financial Economics Letters) "Investment information content in Bollinger Bands?": 비용 조정 후 Bollinger 신호는 일관되게 buy-and-hold를 이기지 못한다. contrarian으로 쓰면 수익성이 나아진다. — [DOI](https://doi.org/10.1080/17446540701206576)
- Bajgrowicz & Scaillet (2012, JFE) "Technical trading revisited":
  - DJIA 1897–2011년에 False Discovery Rate로 data-snooping을 보정했다.
  - 최고 규칙을 사전에 골라낼 수 없다.
  - 낮은 거래비용만으로도 표본 내 성과가 모두 사라진다. — [DOI](https://doi.org/10.1016/j.jfineco.2012.06.001), [SUFE 초록](https://academicnewsletter.sufe.edu.cn/info/356896)
- Brock, Lakonishok & LeBaron (1992, JF): 이동평균과 trading-range breakout 규칙이 DJIA에서 예측력을 보였다는 고전이다. (내용 미확인, 오래된 결과) — [DOI](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x)
- Sullivan, Timmermann & White (1999, JF): bootstrap(White Reality Check)으로 data-snooping을 보정하면 표본 외에서 우위가 약해진다. (내용 미확인) — [DOI](https://doi.org/10.1111/0022-1082.00163)
- Park & Irwin (2007, Journal of Economic Surveys): 기술적 분석 수익성 서베이다. 긍정적 결과가 많지만 data-snooping, 사후 규칙 선택, 비용 추정 문제가 있다는 결론으로 알려져 있다. (수치 미확인) — [DOI](https://doi.org/10.1111/j.1467-6419.2007.00519.x)
- **규칙(Turtle/Donchian)**:
  - System 1: 20일 고가를 돌파하면 진입하고, 10일 저가를 이탈하면 청산한다.
  - System 2: 55일 고가 돌파로 진입하고, 20일 저가 이탈로 청산한다.
  - 포지션 크기와 손절은 20일 ATR로 정한다.
  - 남아공 선물시장에서 이를 검증한 석사논문(UCT 2016)이 있지만, 표본 외 수치는 확인하지 못했다. — [UCT thesis](https://open.uct.ac.za/handle/11427/21754)
- Szakmary, Shen & Sharma (2010, JBF) "Trend-following trading strategies in commodity futures: A re-examination": 상품 선물에서 이중 이동평균과 채널(breakout) 규칙이 수익을 냈다는 재검증으로 알려져 있다. (미확인: 초록을 얻지 못함) — [DOI](https://doi.org/10.1016/j.jbankfin.2009.08.004)

### Inferences
- RSI(2)와 Bollinger의 "단기 과매도 반전" 논리는 1절의 단기 반전, 즉 유동성 공급의 실무 버전이다. 그러니 Nagel과 Dai et al.의 조건(고변동, 저회전)으로 걸러야 한다. 단독 규칙을 그대로 쓰기보다 feature(RSI(2), %b, Donchian 위치)로 앙상블에 넣고 표본 외 hit rate로 판정하는 편이 문헌과 맞는다.
- 규칙 후보를 많이 시험할수록 data-snooping 위험이 커진다. Bajgrowicz-Scaillet식 FDR 보정이나 White Reality Check를 사전 검증 절차에 넣을 만하다.

### Gaps
- Connors RSI(2)를 독립적으로 비용 후 표본 외 검증한 학술 문헌은 찾지 못했다.
- 한국 개별 주식에 Turtle/Donchian을 적용한 연구도 찾지 못했다.

---

## 6. Volatility-managed 전략: Moreira & Muir (2017)와 비판

### Takeaway
변동성이 높을 때 노출을 줄이면 spanning regression에서는 alpha가 난다(Moreira & Muir 2017). 하지만 실시간으로 구현하면 103개 전략 중 다수에서 원래 포트폴리오보다 못하다(Cederburg et al. 2020). 거래비용을 넣어도 살아남지 못한다는 비판(Barroso & Detzel)도 있다. 다요인을 조건부로 결합하면 표본 외 비용 후에도 우위가 있다는 재반론(DeMiguel et al. 2024)이 나왔다. 실무적으로는 alpha 원천보다 위험 관리(포지션 축소, 신호 보류)로 쓰는 것이 안전하다.

### Cited Findings
- **규칙**: 다음 달 노출을 c ÷ (지난달 실현분산)에 맞춘다.
- Moreira & Muir (2017, JF) "Volatility-Managed Portfolios": 시장, value, momentum, profitability, ROE, investment, BAB, carry 모두에서 변동성 관리가 큰 alpha와 Sharpe 상승을 낸다. 변동성 변화가 기대수익 변화로 상쇄되지 않기 때문이다. — [DOI](https://doi.org/10.1111/jofi.12513)
- Cederburg, O'Doherty, Wang & Yan (2020, JFE) "On the performance of volatility-managed portfolios":
  - 103개 주식 전략에서 직접 비교하면 체계적 우위가 없다.
  - spanning regression의 alpha는 실시간으로 구현할 수 없다.
  - 표본 외 버전은 원 포트폴리오보다 확실성등가 수익과 Sharpe가 낮다.
  - 원인은 회귀계수의 구조적 불안정이다. — [DOI](https://doi.org/10.1016/j.jfineco.2020.04.015), [저자 PDF](https://www.lehigh.edu/~xuy219/research/COWY.pdf)
  - CXO 요약에 따르면 103개 중 72개에서 표본 외 성과가 원 포트폴리오보다 낮았다. — [CXO](https://www.cxoadvisory.com/?p=32455)
- Barroso & Detzel (2021, JFE) "Do limits to arbitrage explain the benefits of volatility-managed portfolios?": 변동성 관리 전략이 거래비용을 넘지 못한다는 결론이다. DeMiguel et al. 초록에 그렇게 요약돼 있다. — [DOI](https://doi.org/10.1016/j.jfineco.2021.02.009)
- DeMiguel, Martín-Utrera & Uppal (2024, JF) "A Multifactor Perspective on Volatility-Managed Portfolios": 조건부 다요인 포트폴리오는 표본 외, 비용 후에도 무조건부 버전을 이긴다. 요인 위험가격은 시장 변동성이 높을수록 대체로 낮아진다. — [DOI](https://doi.org/10.1111/jofi.13395)
- Barroso & Santa-Clara(2015)와 Daniel & Moskowitz(2016)는 momentum에 한정해 변동성 관리가 crash를 줄인다고 본다(2절).

### Inferences
- krx-quant는 신호만 내는 엔진이다. 변동성 관리는 "고변동 국면에서 확신도를 낮추거나 flat(관망)을 늘리는" 규칙으로 옮길 수 있다. Cederburg의 경고를 따르면, 이 규칙의 가치는 hit rate가 아니라 손실 꼬리 축소로 평가해야 한다.

### Gaps
- 한국 개별 대형주에 변동성 관리를 적용한 연구는 찾지 못했다.

---

## 7. 한국 특유의 근거: momentum과 reversal, 외국인·기관 수급, 반도체와 미국 연동

### Takeaway
- 한국 개별주의 월간 momentum은 장기 표본(1983–2023)에서 반전이 우세하다. 업종 momentum은 유의하지 않다(Kang, Ryu & Webb 2025). 대형주(KOSPI50)는 momentum을 오히려 약하게 만든다.
- 외국인 순매수는 대체로 과거 수익을 따라가는(feedback) 성격이 강하다. 전일 외국인 순매수는 다음날 시가 갭(close-to-open)과는 양(+)의 관계지만, 장중에는 그 효과가 사라지거나 반전한다.
- 미국 수익률이 다른 나라 수익률을 선행하는 것은 문헌으로 확인된다(Rapach et al. 2013). 한국에서는 미국 야간 정보가 시가에 반영된 뒤 장중에 되돌아간다는 결과가 있다.
- 삼성전자, SK하이닉스와 SOX/NVDA/MU의 연동을 직접 다룬 동료심사 논문은 찾지 못했다.

### Cited Findings
**Momentum과 reversal**
- Kang, Ryu & Webb (2025, Investment Analysts Journal) "Momentum and reversal effects in the Korean stock market":
  - 1983–2023년 한국 주식이 대상이다.
  - 개별주 momentum 포트폴리오는 reversal을 보이고, 업종 momentum은 유의하지 않다.
  - 일부 하위기간에만 momentum이 나타나고, 전체로는 reversal이 우세하다. 위험요인을 통제해도 남는다. — [DOI](https://doi.org/10.1080/10293523.2024.2448054)
- Choi, Choi & Kang (2012, arXiv) "Momentum universe shrinkage effect in price momentum": KOSPI200 하위 유니버스별로 momentum 성과가 다르다. KOSPI50 같은 대형주는 momentum 성과를 떨어뜨린다. 외국인 매매 패턴이 원인 후보다. — [arXiv](https://arxiv.org/abs/1211.6517)
- Sim & Kim (2021, Journal of Derivatives and Quantitative Studies): 한국 momentum의 기간 구조는 주로 직전 2개월 반전이 이월된 결과다. — [DOI](https://doi.org/10.1108/JDQS-02-2021-0005)
- Jung & Kang (2021, JDQS): variance ratio 검정에서 2000년 이후 KOSPI와 KOSDAQ에 단기 평균회귀가 있다. 이 평균회귀는 거래량 변화 효과를 빼면 크게 줄어든다. — [DOI](https://doi.org/10.1108/JDQS-01-2021-0003)
- KAIST 연구(salience): 전통적 momentum은 성과가 저조하고 장기 반전을 겪는다. idiosyncratic, rank, sign momentum은 안정적이다. — [KAIST](https://koasas.kaist.ac.kr/handle/10203/297600)
- 2011년 국내 연구(1980–2009): 외환위기 전에는 반전, 이후에는 momentum이 나타났다. 검색 요약 기준이며 서지 세부는 미확인. — [earticle](https://www.earticle.net/Article/A164768)
- Chui, Titman & Wei (2010, JF) "Individualism and Momentum around the World": Hofstede 개인주의 지수가 높을수록 momentum 이익이 크다. 한국처럼 개인주의 점수가 낮은 아시아 시장에서 momentum이 약하다는 해석의 근거다. 한국의 점수 자체는 미확인. — [DOI](https://doi.org/10.1111/j.1540-6261.2009.01532.x)
- Griffin, Ji & Martin (2003, JF): 세계 각국의 momentum 이익은 크고 경기 상태와 무관하다. 1–5년 뒤에 반전한다. — [DOI](https://doi.org/10.1046/j.1540-6261.2003.00614.x)

**외국인·기관 수급**
- Choe, Kho & Stulz (1999, JFE) "Do foreign investors destabilize stock markets? The Korean experience in 1997":
  - 1996.11–1997년 말 주문·체결 데이터를 썼다.
  - 위기 이전 외국인에게 positive feedback trading과 herding이 강했다.
  - 외국인의 대량 매도 뒤에 음(-)의 abnormal return은 없었다. 즉 시장 불안정을 일으켰다는 증거가 없다. (오래된 결과) — [DOI](https://doi.org/10.1016/S0304-405X(99)00037-9), [NBER](https://www.nber.org/papers/w6661)
- Choe, Kho & Stulz (2005, RFS) "Do Domestic Investors Have an Edge?": 외국인 운용사는 중대형 거래에서 국내 운용사보다 매수 시 21bp, 매도 시 16bp 불리하다. 거래 직전에 가격이 외국인에게 불리하게 움직인다. (오래된 결과) — [RePEc](https://ideas.repec.org/a/oup/rfinst/v18y2005i3p795-829.html), [NBER](https://www.nber.org/papers/w10502)
- Richards (2005, JFQA) "Big Fish in Small Ponds": 아시아 신흥시장(한국 포함) 외국인 매매의 가격 영향과 feedback 성향을 다룬다. (세부 미확인) — [DOI](https://doi.org/10.1017/S0022109000001721)
- 김수경·변영태 (2011, 경영과 정보연구 30(4)) "외국인 및 기관투자자의 순매수강도와 주식수익률 간의 관계": KOSPI 일별 2003.1–2011.4 자료다.
  - 전일 외국인과 기관의 순매수강도는 당일 종가-종가 수익률에 음(-), 당일 종가-시가(close-to-open) 수익률에 양(+)의 영향을 준다.
  - 외국인 효과는 시가가 형성된 뒤 사라진다.
  - 국문 초록과 영문 초록의 기술이 일부 엇갈린다. — [earticle](https://m.earticle.net/Article/A164764)
- 서병덕 외 (2011, 상업경영연구 25(4)): 외국인 순매수는 장중 수익률보다 비거래시간(밤사이) 수익률과 더 강하게 연관된다. 저자들은 외국인이 글로벌 정보에 더 비중을 둔다고 해석한다. — [earticle](https://www.earticle.net/Article/A161294)
- 변영태 (2012, 경영과 정보연구 31(4)): 2004–2012년 자료다. 외국인 순매수강도의 예상 밖 변동은 전일분이 다음날 변동성을 낮추고, 당일분은 당일 변동성을 높인다. — [earticle](https://www.earticle.net/Article/A191207)
- IJMF 게재 논문(2020–2021, 저자 미확인) "Investor behavior, stock returns and CDS spreads": VAR 분석이다. 국내 기관 순매수는 다음날 수익률을 양(+)으로, 개인 순매수는 음(-)으로 예측한다. 외국인은 수익률이 오른 다음날 더 많이 산다(return chasing). — [DOI](https://doi.org/10.1108/IJMF-01-2020-0022)
- Zenodo 프리프린트(2026.2): 외국인 수급의 예측력이 2–4일 시계에서 최대이고 Sharpe가 9.24라고 주장한다. 동료심사를 받지 않았고 수치가 비현실적으로 높다. 신뢰하지 않는다. — [Zenodo](https://zenodo.org/records/18598116)

**미국 연동과 반도체**
- Rapach, Strauss & Zhou (2013, JF) "International Stock Return Predictability: What Is the Role of the United States?": 미국의 시차 월간 수익률이 여러 선진국 수익률을 예측한다. 반대 방향은 약하다. 정보가 점진적으로 확산된다는 해석이다.
  - 후속 연구는 이 관계가 시기에 따라 변하고 미국 경기침체기에 집중된다고 지적한다. 출처는 검색 요약. — [DOI](https://doi.org/10.1111/jofi.12041), [후속: Aye et al.](https://repository.up.ac.za/handle/2263/59374)
- Lo & MacKinlay (1990): 대형주가 소형주를 선행한다(1절). 같은 업종 안 선행·후행의 일반 근거다. — [DOI](https://doi.org/10.1093/rfs/3.2.175)
- Cohen & Frazzini (2008, JF) "Economic Links and Predictable Returns": 고객사 수익률이 공급사 수익률을 시차를 두고 예측한다. 수치는 미확인. — [DOI](https://doi.org/10.1111/j.1540-6261.2008.01379.x)
- 국내 연구(2010, KOSPI200 선물): 미국 야간 시장에서 한국으로 정보가 넘어오는 효과가 있다. 시가에서 종가 사이에는 유의한 가격 반전이 있다. 검색 요약 기준, 서지 미확인. — [earticle](https://m.earticle.net/Article/A166629)
- 중앙대 연구(2019): KOSPI 종목의 overnight 수익률이 높으면 2–3거래일은 수익률이 높고, 장기에는 낮다. — [CAU](https://scholarworks.bwise.kr/cau/handle/2019.sw.cau/33122)
- arXiv 프리프린트(2026) "Preying on Leveraged ETFs": 삼성전자와 SK하이닉스를 대상으로, 개장 전 미국 SMH, QQQ, SPY 수익률 충격과 overnight 반전을 2026-05-27 국내 상품 출시 전후로 비교한다. 표 캡션만 확인했고 결론은 미확인. — [arXiv](https://arxiv.org/abs/2608.03703)

### Inferences
- 세 종목은 KOSPI 최대형주다. 한국 문헌상 개별주 월간 momentum은 약하거나 반전한다. 대형주는 momentum을 더 약하게 만든다. 반대로 회전율이 높은 대형주는 short-term momentum이 나온다는 결과도 있다(1절). 결국 한국 대형주에서 1–4주 방향성의 부호는 미리 정하지 말고 데이터로 정해야 한다.
- 외국인 순매수는 "다음날 시가 갭" 예측에는 정보가 있다. 하지만 장중과 다일 시계에서는 사라지거나 반전한다는 결과가 많다. 그래서 스윙 예측에서는 하루치 순매수보다 누적(5–20일) 순매수의 지속성이나 반전을 따로 검증해야 한다.
- 미국 반도체(NVDA, MU, SOX)의 야간 수익률은 시가 갭의 강한 설명변수일 가능성이 높다. 국내 연구들은 시가 이후 장중 반전을 시사한다. 그러므로 "시가 갭"과 "시가→종가"를 서로 다른 예측 대상으로 나누는 것이 문헌과 맞는다.

### Gaps
- 프로그램 매매(차익·비차익)와 외국인 KOSPI200 선물 순매수가 다일 수익률을 예측하는지에 대한 최근 연구는 검색 한도 때문에 찾지 못했다.
- Park & Yi(2011)와 Fung et al.(2010)(미국 수익률과 한국 장중 수익률의 음의 관계)은 검색 요약 속 인용으로만 봤다. 원문 URL을 확보하지 못했다.
- 삼성전자, SK하이닉스와 SOX/NVDA/MU 연동을 직접 다룬 동료심사 논문은 찾지 못했다. 시장 기사의 "하이닉스 1% 상승 시 SOX 상승 확률 77%" 같은 수치는 방법론이 없어 제외했다.
- KCMI(자본시장연구원) 보고서는 이번 조사에서 확인하지 못했다.

---

## 8. 감쇠(decay)와 최근 연구(2023–2026)

### Takeaway
학술 발표 뒤 이상현상 수익은 평균 58% 줄어든다(McLean & Pontiff 2016). 단기 반전, pairs, PEAD, 지수 편입 효과는 모두 2000년대 이후 크게 약해졌다. 최근 연구의 흐름은 셋이다. 단기 신호를 여러 개 결합하고 거래 규칙으로 비용을 줄이면 net alpha가 남는다(Blitz et al. 2023). 반전의 크기는 변동성과 회전율에 달려 있다(Dai et al. 2024). 한국은 장기적으로 반전이 우세하다(Kang et al. 2025).

### Cited Findings
- McLean & Pontiff (2016, JF): 97개 예측변수의 포트폴리오 수익이 표본 외에서 26%, 발표 후에 58% 줄어든다. 표본 내 수익이 컸던 변수일수록 더 많이 줄어든다. — [DOI](https://doi.org/10.1111/jofi.12365)
- Jacobs & Müller (2020, JFE) "Anomalies across the globe: Once public, no longer existent?": 발표 후 감쇠가 미국에서만 뚜렷하고 해외 시장에서는 약하다는 결론으로 알려져 있다. (미확인: 초록을 얻지 못함) — [DOI](https://doi.org/10.1016/j.jfineco.2019.06.004)
- Chordia, Subrahmanyam & Tong (2014, JAE): 고유동성 시대(decimalization 이후)에 이상현상 수익이 줄었다. (미확인: 세부 수치) — [DOI](https://doi.org/10.1016/j.jacceco.2014.06.001)
- 전략별 감쇠 요약:
  - 단기 반전과 pairs: 2002년 이후 비용 후 거의 0이다. [Do & Faff 2012](https://doi.org/10.1111/j.1475-6803.2012.01317.x)
  - pairs: 2009년 이후 거래 기회가 줄었다. [Rad et al. 2016](https://doi.org/10.1080/14697688.2016.1164337)
  - stat-arb Sharpe: 1.44에서 0.9로 떨어졌다(2003–07). [Avellaneda & Lee 2010](https://doi.org/10.1080/14697680903124632)
  - PEAD: 대형주에서 2006년 이후 0이다. [Martineau 2022](https://ideas.repec.org/a/now/jnlcfr/104.00000122.html)
  - S&P 편입 효과: 7.4%에서 1% 미만으로 줄었다. [Greenwood & Sammon 2025](https://doi.org/10.1111/jofi.13410)
- Blitz, Hanauer, Honarvar, Huisman & van Vliet (2023, FAJ) "Beyond Fama-French Factors: Alpha from Short-Term Signals":
  - short-term reversal, short-term momentum, 단기 애널리스트 수정, 단기 위험, 월별 계절성을 합성한다.
  - 글로벌 유동성 유니버스에서 비용을 줄이는 매수·매도 규칙을 쓰면 net alpha가 난다.
  - 표본 외와 발표 후, 여러 지역, long-only에서 유지된다. 수일의 실행 지연에도 견딘다. — [DOI](https://doi.org/10.1080/0015198X.2023.2173492)
- Dai et al. (2024, FAJ): 반전 속도와 크기는 변동성과 회전율에 달려 있고, 미국 밖에서도 같다(1절). — [DOI](https://doi.org/10.1080/0015198X.2023.2292534)
- DeMiguel et al. (2024, JF): 조건부 다요인 변동성 관리는 표본 외, 비용 후에도 우위가 있다(6절). — [DOI](https://doi.org/10.1111/jofi.13395)
- Kang, Ryu & Webb (2025): 한국 1983–2023년은 반전이 우세하다(7절). — [DOI](https://doi.org/10.1080/10293523.2024.2448054)
- 2025년 PEAD 논쟁(4절). — [UCLA Anderson Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again/)

### Inferences
- 남아 있는 우위는 "단일 고전 규칙"보다 "여러 단기 신호의 결합과 비용 관리"에 있다. 이는 krx-quant의 앙상블 구조와 맞는다.
- 한국에서는 발표 후 감쇠가 미국보다 약할 수 있다(Jacobs & Müller, 미확인). 하지만 세 종목은 외국인과 기관이 가장 많이 보는 종목이라 감쇠가 더 빠를 가능성도 있다. 추정일 뿐이다.

### Gaps
- 2024–2026년 trend-following(CTA) 성과나 TSMOM 재검증 논문은 검색 한도 때문에 확인하지 못했다.
- 한국 시장 이상현상의 발표 후 감쇠를 직접 측정한 연구는 찾지 못했다.

---

## krx-quant 적용 메모

전제: 대상은 세 종목(005930, 000660, 009150)이고 신호만 낸다. 데이터는 1분봉 1년, 일봉, 투자자별 수급 10년, KOSPI200 선물, 달러 선물, 미국 야간 NVDA/MU다. 판정 기준은 up/flat/down hit rate다. 하락은 인버스 상품으로 표현한다.

**세 종목 시계열 환경에 맞는 정도**

| 전략 | 적합도 | 이유 |
|---|---|---|
| 미국 반도체 야간 수익률 → 시가 갭 | 높음 | Rapach et al. 2013과 국내 연구가 있다. 데이터(NVDA/MU)가 이미 있다. 다만 장중 반전 결과에 유의해야 한다. |
| 외국인·기관 수급(일별·누적) | 높음 | 한국 고유의 데이터 우위다. close-to-open에는 양(+), 장중과 다일 시계에서는 소멸이나 반전한다는 결과가 있어 시계별로 따로 검증해야 한다. |
| residual 단기 반전 / short-term momentum (1–5일) | 중간 | 시장·반도체 요인 잔차 기준으로 쓴다. 회전율과 변동성으로 조건화한다(Medhat-Schmeling, Dai et al., Nagel). 부호는 데이터로 정한다. |
| 삼성전자–SK하이닉스(–삼성전기) 상대강도 | 중간 | pairs를 "어느 종목이 N일 동안 더 오를까"로 바꾼다. 공통 요인이 강해 잔차가 작고, 2002년 이후 문헌상 감쇠가 크다. |
| 추세(TSMOM, 52주 고가, Donchian) | 낮음~중간 | 단일 자산의 통계적 근거가 약하다(Huang et al.). 한국 개별주는 반전이 우세하다. 국면 변수로만 쓴다. |
| 이벤트 flag(잠정실적, 옵션 만기, 지수 리밸런싱, Micron 실적) | 보조 | 단독 alpha는 작다(PEAD 소멸, 편입 효과 감쇠). 변동성과 노이즈 국면 표시로 쓴다. |
| 변동성 관리 | 보조 | alpha 원천이 아니다(Cederburg). 고변동이나 panic state에서 확신도를 낮추고 flat을 늘리는 규칙으로 쓴다. |
| RSI(2), Bollinger 단독 규칙 | 낮음 | 독립적인 표본 외 근거가 없거나 부정적이다. feature로만 쓴다. |

**필요한 새 일간 예측 종류(제안)**
1. **D+1 시가 갭 방향**(전일 종가 → 다음날 시가). 입력은 미국 야간 NVDA/MU/SOX, 달러 선물, 전일 외국인·기관 순매수다. 문헌상 신호가 가장 뚜렷한 구간이다.
2. **D+1 시가→종가 방향**. 갭의 반전과 지속을 예측한다. 국내 연구가 장중 반전을 시사하므로 1과 분리해야 한다.
3. **5거래일(또는 10거래일) 누적 수익률 방향**. 스윙의 기본 단위다. flat 구간은 종목별 일간 변동성 × √h × k로 정한다. 입력은 누적 수급(5·20일), residual 수익률(1–5일), 52주 고가 근접도, 회전율과 변동성 국면이다.
4. **상대수익 방향(삼성전자 vs SK하이닉스, 5거래일)**. pairs와 stat-arb의 시계열 대체물이다. 롱 종목 선택에 쓴다.

**검증 시 주의(문헌에서 나온 것)**
- 일봉 10년은 종목당 약 2,450일이다. 겹치지 않는 5일 구간은 약 490개라 통계적 검정력이 낮다. 1분봉 1년치는 스윙 학습에 부족하다.
- 시계가 겹치는 라벨에는 purge와 embargo를 둔 교차검증이 필요하다. 규칙 후보를 많이 시험하면 FDR이나 Reality Check로 data-snooping을 보정해야 한다(Bajgrowicz & Scaillet, Sullivan et al.).
- 2002년 이후 감쇠가 크고 2008년 같은 고변동기에만 반짝 살아나는 패턴이 반복된다. 그래서 hit rate는 국면별(VKOSPI 구간별)로도 따로 보고해야 한다.
