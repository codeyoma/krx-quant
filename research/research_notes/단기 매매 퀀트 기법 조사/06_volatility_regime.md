# 변동성 예측과 국면(regime) 판별: 단기 전략의 조건부 운용

기준일 2026-10-17. 투자 조언이 아니라 연구 메모다. 표기: **[2015 이전]** 은 오래된 결과, **(미검증)** 은 원문·수치를 직접 확인하지 못한 것. DOI는 모두 Crossref 메타데이터(제목·저자·학술지)로 확인했다.

## 1. 실현변동성 모델: HAR-RV 계열, GARCH 계열, range 기반 추정량

### Takeaway
단기 변동성 예측의 기본값은 여전히 HAR-RV(선형, 계수 3–4개)다. 점프 분리(HAR-CJ), 측정오차 보정(HARQ), 하방 semivariance는 소폭 개선을 주지만 재현 결과는 엇갈린다. range 기반 추정량(Parkinson, Garman–Klass, Rogers–Satchell, Yang–Zhang)은 OHLC만으로 계산되고 close-to-close보다 훨씬 효율적이라 1분봉 엔진에 맞는다.

### Cited Findings
- **HAR-RV**: 일·주·월 평균 RV를 단순 회귀해 long memory를 근사한다. Corsi, "A Simple Approximate Long-Memory Model of Realized Volatility", J. Financial Econometrics 7(2), 2009 [2015 이전] — [DOI](https://doi.org/10.1093/jjfinec/nbp001)
- **RV의 바탕**: 고빈도 수익률 제곱합으로 변동성을 측정하고 단순 장기기억 모형으로 예측하면 GARCH류보다 낫다는 결과. Andersen, Bollerslev, Diebold, Labys, Econometrica 71(2), 2003 [2015 이전] — [DOI](https://doi.org/10.1111/1468-0262.00418)
- **HAR-J / HAR-CJ**: bipower variation으로 RV를 연속 성분과 점프 성분으로 나누면, 점프 성분은 지속성이 약하고 연속 성분이 미래 변동성을 이끈다. Andersen, Bollerslev, Diebold, "Roughing It Up", REStat 89(4), 2007 [2015 이전] — [DOI](https://doi.org/10.1162/rest.89.4.701)
- **HARQ**: RV 측정오차(realized quarticity로 추정)가 클 때 직전 RV 계수를 줄이는 시변 계수. S&P 500과 Dow Jones 구성 종목에서 HAR 대비 정확도 개선. Bollerslev, Patton, Quaedvlieg, "Exploiting the errors", J. Econometrics 192(1), 2016 — [DOI](https://doi.org/10.1016/j.jeconom.2015.10.007); [Maastricht 초록](https://cris.maastrichtuniversity.nl/en/publications/exploiting-the-errors-a-simple-approach-for-improved-volatility-f/)
- HARQ 재현은 엇갈린다. 한 석사 논문은 HAR·CHAR를 뚜렷이 못 이겼다고 보고 — [Univie thesis](https://utheses.univie.ac.at/detail/42627). 중국 지수 LogHARQ는 RQ가 높을 때 개선이 크다고 보고 — [PKU WP](https://en.nsd.pku.edu.cn/docs/2018-10/20181019194801055709.pdf)
- **Semivariance / signed jump**: 하방 semivariance(음의 수익률 제곱합)가 상방보다 미래 변동성을 더 잘 예측한다. Patton & Sheppard, "Good Volatility, Bad Volatility", REStat 97(3), 2015 — [DOI](https://doi.org/10.1162/REST_a_00503)
- **GARCH 계열**: GARCH(1,1) Bollerslev 1986 [2015 이전] — [DOI](https://doi.org/10.1016/0304-4076(86)90063-1). 330개 모형 비교에서 환율은 GARCH(1,1)을 못 이겼고, IBM 주식은 leverage(비대칭) 모형이 나았다. Hansen & Lunde 2005 [2015 이전] — [DOI](https://doi.org/10.1002/jae.800)
- **Realized GARCH**: 수익률과 실현 측정치(RV 등)를 측정 방정식으로 함께 모델링해 일간 GARCH에 고빈도 정보를 넣는다. Hansen, Huang, Shek, J. Applied Econometrics 27(6), 2012 [2015 이전] — [DOI](https://doi.org/10.1002/jae.1234)
- **Rough volatility**: 로그 변동성은 Hurst H≈0.1 수준의 거친(rough) 과정처럼 움직이고, 이것이 HAR식 예측이 잘 맞는 이유와 연결된다. Gatheral, Jaisson, Rosenbaum, Quantitative Finance 18(6), 2018 — [DOI](https://doi.org/10.1080/14697688.2017.1393551)
- **Range 추정량** [모두 2015 이전]:
  - Parkinson 1980: 고가·저가만 사용 — [DOI](https://doi.org/10.1086/296071)
  - Garman & Klass 1980: OHLC를 함께 쓰는 추정량 — [DOI](https://doi.org/10.1086/296072)
  - Rogers & Satchell 1991: drift(추세)에 영향받지 않는 추정량 — [DOI](https://doi.org/10.1214/aoap/1177005835)
  - Yang & Zhang 2000: drift와 시가 갭(opening jump)에 모두 영향받지 않는 최소분산 결합 추정량 — [DOI](https://doi.org/10.1086/209650)
- **예측 평가 손실**: 진짜 변동성 대신 잡음 섞인 proxy(RV, range)로 평가할 때도 순위가 보존되는 손실은 MSE와 QLIKE다. Patton, J. Econometrics 160(1), 2011 [2015 이전] — [DOI](https://doi.org/10.1016/j.jeconom.2010.03.034)

### Inferences
- 효율 배수(Parkinson 약 5배, GK 약 7배 등)는 연속 거래·무잡음 가정에서 나온 값이다. 1분봉 체결가 range는 bid-ask bounce로 부풀고, 이산 관측 때문에 극값을 놓쳐 줄어든다. 중간가 range를 쓰는 krx-quant의 현재 정의(docs/design.md 5장 "변동폭")가 이 문제를 덜어 준다.
- 일·스윙 지평에서는 KRX의 밤사이 갭이 크다. 일간 변동성에는 시가 갭을 따로 다루는 Yang–Zhang이 맞다.
- GARCH는 1분 수익률에 바로 쓰면 장중 계절성(2절) 때문에 지속성이 왜곡된다. 계절성을 먼저 제거해야 한다.

### Gaps
- Range 추정량의 효율 배수(5배·7.4배·Yang–Zhang 최대치)는 원문 표를 직접 확인하지 못했다(관행적으로 인용되는 값).
- Oxford-Man Realized Library 문서와 현재 운영 여부(중단 여부)를 확인하지 못했다(검색 결과 없음).

## 2. 장중 변동성 계절성과 점프 탐지

### Takeaway
장중 변동성은 U자형 계절 패턴이 강하다. 이를 제거하지 않으면 분 단위 모형이 지속성을 잘못 추정한다. 실무형 해법은 일간 분산 × 장중 계절 계수 × 장중 확률 성분의 곱셈 구조다. 점프는 bipower variation(BNS)과 Lee–Mykland 검정으로 분 단위에서 싸게 판별할 수 있다.

### Cited Findings
- 고빈도 절대수익률의 자기상관은 장중 주기성(intraday periodicity) 때문에 크게 왜곡된다. 주기성을 걸러내면 일간 변동성 지속성과 일관된 장기기억이 드러난다. Andersen & Bollerslev, J. Empirical Finance 4(2–3), 1997 [2015 이전] — [DOI](https://doi.org/10.1016/S0927-5398(97)00004-2)
- **Multiplicative component GARCH**: 장중 분산 = 일간 분산 × 장중 계절(diurnal) 계수 × 장중 확률 성분(GARCH). 미국 주식 10분 수익률에 적용. Engle & Sokalska, J. Financial Econometrics 10(1), 2012 [2015 이전] — [DOI](https://doi.org/10.1093/jjfinec/nbr005)
- **Bipower variation**: 인접 절대수익률의 곱의 합으로, 점프에 강건한 적분분산 추정량이다. RV − BV가 점프 변동을 추정한다. Barndorff-Nielsen & Shephard 2004 [2015 이전] — [DOI](https://doi.org/10.1093/jjfinec/nbh001); 점프 검정 2006 [2015 이전] — [DOI](https://doi.org/10.1093/jjfinec/nbi022)
- **Lee–Mykland 검정**: 각 수익률을 국소 bipower 변동성으로 표준화하고, 그 최댓값의 극값(Gumbel) 분포로 문턱을 정해 점프의 **시점**을 장중에 찾는다. Lee & Mykland, RFS 21(6), 2008 [2015 이전] — [DOI](https://doi.org/10.1093/rfs/hhm056)
- KRX 만기일: KOSPI200 파생상품 만기일에 현물시장의 가격·변동성·거래량 효과가 뚜렷했다. 정산가가 마지막 10분 단일가 매매로 정해지는 구조와 연결된다. Park & Lim, KDI Journal of Economic Policy 25(2), 2003 [2015 이전] — [KDI](https://kdijep.org/v.25/2/137/Expiration+Day+Effects+in+Korean+Stock+Market+Wag+the+Dog+); [KoreaScience PDF](https://koreascience.kr/article/JAKO200325758343243.pdf)

### Inferences
- krx-quant의 Mandelbrot은 "전 거래일 같은 30분 시간대 범위"만 계절 정보로 쓴다. 하루치 계절 계수는 잡음이 크다. Andersen–Bollerslev·Engle–Sokalska처럼 여러 날(예: 20–60거래일) 같은 분(minute-of-day)의 평균으로 계수를 만들고, 만기일·동시호가 구간은 따로 두는 것이 정석이다.
- 점프 직후 변동성은 연속 성분보다 빨리 꺼진다(ABD 2007). Mandelbrot의 알려진 약점인 "큰 움직임 뒤 곧바로 잦아듦"(docs/design.md 표)을 겨냥한 직접적인 처방이다.

### Gaps
- 최근(2024–2026) KRX 장중 계절성과 만기일 효과 실증은 찾지 못했다. 분 단위 KOSPI200 만기 연구(Choi & Ryu 2005로 보이는 것)는 출처 URL을 특정하지 못했다.
- KOSPI200 위클리 옵션 만기의 장중 효과 연구는 찾지 못했다.

## 3. 국면 모델: Markov switching, HMM, BOCPD, statistical jump model, 클러스터링

### Takeaway
국면 조건화가 표본 외(OOS)에서 도움이 된다는 증거는 주로 **일간 자산배분**(위험 노출 줄이기)에서 나온다. 수수료와 지연을 넣은 OOS에서 HMM이 정적 전략을 이기고, jump penalty로 국면 지속성을 강제한 statistical jump model(JM)이 HMM을 이긴다는 보고가 있다. 분 단위 방향 예측에서 같은 효과를 보인 동료심사 결과는 찾지 못했다.

### Cited Findings
- **Hamilton(1989) Markov switching**: 관측되지 않는 이산 국면이 Markov 연쇄를 따르며 평균·분산을 바꾸는 모형의 원형(GNP 성장률). [2015 이전] — [DOI](https://doi.org/10.2307/1912559)
- **리뷰**: 국면 모형은 두꺼운 꼬리, 변동성 군집, 시변 상관을 잡는다. Ang & Timmermann, Annual Review of Financial Economics 4, 2012 [2015 이전] — [DOI](https://doi.org/10.1146/annurev-financial-110311-101808)
- **HMM 자산배분**: 경제 변수에 HMM을 맞춘 국면 기반 동적 전략이 정적 대안보다 유의하게 효과적이었다. Kritzman, Page, Turkington, FAJ 68(3), 2012 [2015 이전] — [DOI](https://doi.org/10.2469/faj.v68.n3.3)
- 적응적으로 추정한 2-state Gaussian HMM 전략이 수수료, 수익률 사전지식 없음, 국면 반응 지연을 넣은 OOS에서 리밸런싱 벤치마크를 이겼다(Nystrup 석사논문) — [DTU](https://www2.imm.dtu.dk/pubdb/pubs/6808-full.html). 학술지판: Nystrup et al., "Regime-Based Versus Static Asset Allocation", JPM 42(1), 2015 — [DOI](https://doi.org/10.3905/jpm.2015.42.1.103)
- **Statistical jump model**: 모수 적합과 동적계획법 국면 배정을 번갈아 하고, 국면 전환마다 벌점을 준다. Bemporad, Breschi, Piga, Boyd, "Fitting jump models", Automatica 96, 2018 — [DOI](https://doi.org/10.1016/j.automatica.2018.06.022). HMM 학습에 jump 벌점을 넣어 국면을 오래 지속시킨다. Nystrup, Lindström, Madsen, ESWA, 2020 — [DOI](https://doi.org/10.1016/j.eswa.2020.113307)
- **JM 표본 외 성과**: 미국·독일·일본 지수(1990–2023)에서 수수료와 거래 지연을 넣은 OOS 결과, JM 기반 위험 축소 전략이 HMM 기반 전략과 buy-and-hold보다 변동성, 최대낙폭, Sharpe에서 일관되게 나았다. 벌점 λ는 전략 성과를 목표로 한 시계열 교차검증으로 고른다. Shu, Yu, Mulvey, J. Asset Management, 2024 — [arXiv](https://arxiv.org/abs/2402.05272); [DOI](https://doi.org/10.1057/s41260-024-00376-x)
- **확장**: "mode loss"를 더한 continuous JM은 기존 regime-switching보다 낫고, 국면이 불균형하고 이력이 짧을 때 차이가 크다. Aydinhan, Kolm, Mulvey, Shu, Annals of Operations Research — [SSRN](https://papers.ssrn.com/abstract=4556048). 팩터 배분 응용: Shu & Mulvey 2024 — [arXiv](https://arxiv.org/abs/2410.14841)
- **BOCPD**: 현재 국면 길이(run length)의 사후분포를 매 관측마다 재귀로 갱신하는 정확한 온라인 변화점 탐지. Adams & MacKay 2007 [2015 이전] — [arXiv](https://arxiv.org/abs/0710.3742)
- BOCPD에 score-driven 모형을 결합하면 NASDAQ 주문흐름의 OOS 예측이 좋아진다. 다만 매매 응용은 향후 과제로 남겼다. Tsaknaki, Lillo, Mazzarisi 2023 — [arXiv](https://arxiv.org/abs/2307.02375). 자기회귀 BOCPD 확장 — [arXiv](https://arxiv.org/abs/2407.16376)
- **변화점으로 추세/역추세 전환**: Deep Momentum Network에 온라인 changepoint 모듈을 붙여 느린 추세추종과 빠른 평균회귀 사이를 오가게 했다. 1995–2020 백테스트에서 Sharpe가 약 1/3, 2015–2020에서 약 2/3 개선됐다. Wood, Roberts, Zohren, J. Financial Data Science, 2022 — [arXiv](https://arxiv.org/abs/2105.13727); [DOI](https://doi.org/10.3905/jfds.2021.1.081)

### Inferences
- JM의 벌점 λ=0이면 시간 정보 없이 가장 가까운 중심에 배정하는 클러스터링(k-means 유사)이 된다. 즉 "클러스터링 기반 국면"은 JM의 특수한 경우로 보고, λ를 교차검증으로 고르는 편이 깔끔하다.
- 4국면 HMM은 분 단위에서 국면이 자주 뒤집히기 쉽다. JM 벌점이나 최소 체류시간이 "깜빡임"을 줄이는 직접적인 수단이다.
- 근거 대부분이 일간 지수 데이터, 수십 년 표본이다. 1년치 1분봉에서 같은 이득이 날지는 krx-quant의 사전 검증(prevalidate)으로만 확인할 수 있다.

### Gaps
- 분 단위(intraday) 방향 적중률을 국면 조건화로 올렸다는 동료심사 OOS 연구는 찾지 못했다.
- Shu et al.의 구체 수치(Sharpe, MDD)는 초록에 없어 옮기지 못했다.
- "Markov-switching으로 주식/현금을 오가 변동성 약 41% 감소" 같은 수치는 저자를 특정하지 못해 제외했다.

## 4. 추세 vs 평균회귀 판정: Hurst, variance ratio, efficiency ratio, ADX

### Takeaway
Variance ratio(VR)는 검정 이론이 탄탄하다. 고전 Hurst(R/S)는 단기 의존성 때문에 편향되며, 이를 보정하면 장기기억 증거가 약해진다. Efficiency ratio와 ADX는 실무에서 널리 쓰이지만 학술적 OOS 근거를 찾지 못했다. 근거가 있는 것은 "변화점·변동성 상태에 따라 모멘텀을 끄거나 줄이는 것"이다.

### Cited Findings
- **Variance ratio 검정**: 주간 지수 수익률은 random walk를 기각한다(양의 자기상관). Lo & MacKinlay, RFS 1(1), 1988 [2015 이전] — [DOI](https://doi.org/10.1093/rfs/1.1.41)
- **Modified R/S**: 단기 의존성을 보정한 R/S로는 주가의 장기기억 증거가 약하다. 고전 R/S(Hurst)는 단기 자기상관에 편향된다. Lo, Econometrica 59(5), 1991 [2015 이전] — [DOI](https://doi.org/10.2307/2938368)
- **장중 모멘텀**: 미국 시장에서 장 시작 30분 수익률이 마지막 30분 수익률을 예측한다. Gao, Han, Li, Zhou, JFE 129(2), 2018 — [DOI](https://doi.org/10.1016/j.jfineco.2018.05.009)
- **시계열 모멘텀(스윙 지평)**: 선물 자산 전반에서 과거 1–12개월 수익률이 이후 수익률을 예측한다. Moskowitz, Ooi, Pedersen, JFE 104(2), 2012 [2015 이전] — [DOI](https://doi.org/10.1016/j.jfineco.2011.11.003)
- **모멘텀 붕괴는 상태 의존적**: 하락장 뒤 고변동 "panic" 상태에서 시장이 반등할 때 크게 무너진다. 평균·분산 예측으로 노출을 조절하면 성과가 개선된다. Daniel & Moskowitz, JFE 122(2), 2016 — [DOI](https://doi.org/10.1016/j.jfineco.2015.12.002)
- 모멘텀의 위험은 예측 가능하고, 이를 관리하면 붕괴가 거의 사라지며 Sharpe가 거의 두 배가 된다. Barroso & Santa-Clara, JFE 116(1), 2015 — [DOI](https://doi.org/10.1016/j.jfineco.2014.11.010)
- 변화점 탐지로 추세/역추세를 전환해 Sharpe를 개선한 사례(3절 Wood et al.) — [arXiv](https://arxiv.org/abs/2105.13727)
- Hurst 기반 장중 역추세 규칙의 예시와 "연 121%" 같은 주장은 플랫폼 게시물이며 검증된 근거가 아니다 — [StockSharp 예제](https://doc.stocksharp.com/en/api-examples/0228_Hurst_Exponent_Reversion)

### Inferences
- 30–60분 창의 1분 수익률로 계산한 Hurst는 표본이 너무 작아 잡음이 크다. 같은 창이라면 VR(q)(예: q=5, 15)와 efficiency ratio(|순변화| ÷ Σ|변화|)가 더 안정적이고, 누적합으로 O(1)에 갱신된다.
- Gauss가 전체 기간에서 되돌림 κ=0을 배운 것은 "평균적으로 역추세가 없다"는 뜻일 뿐, 낮은 ER·VR<1 구간에 조건부 되돌림이 있을 가능성은 남아 있다. 국면별로 나눠 다시 학습해 볼 가치가 있다.

### Gaps
- Kaufman efficiency ratio와 Wilder ADX 필터의 동료심사 OOS 증거를 찾지 못했다(실무서 중심).
- KRX에서 장중 모멘텀(Gao et al. 방식)이 재현되는지는 확인하지 못했다.

## 5. 변동성 타기팅과 vol-managed 사이징

### Takeaway
"예측 변동성에 반비례해 노출을 정한다"는 주식처럼 leverage effect가 있는 위험자산에서 Sharpe를 올리고, 모든 자산군에서 왼쪽 꼬리를 줄인다. 다만 실시간으로 구현 가능한 OOS 버전은 원래 포트폴리오를 못 이기는 경우가 많다는 반론이 강하다. 가장 확실한 이득은 꼬리 위험 축소다.

### Cited Findings
- 변동성이 높을 때 노출을 줄이면 시장·가치·모멘텀·수익성 팩터와 통화 캐리에서 알파가 생긴다. 변동성 변화가 기대수익률 변화로 상쇄되지 않기 때문이다. Moreira & Muir, J. Finance 72(4), 2017 — [DOI](https://doi.org/10.1111/jofi.12513). 시장 포트폴리오 알파 4.9%, appraisal ratio 0.33, Sharpe 약 25% 증가는 **2016 working paper 수치**다(최종판 수치 미확인) — [NBER w22208](https://www.nber.org/papers/22208)
- 60개 자산(일부 1926–2017)에서 변동성 타기팅은 주식·크레딧에서만 Sharpe를 올렸다(leverage effect). 채권·통화·원자재는 효과가 미미했다. 대신 모든 자산군에서 극단 수익률 확률과 최대낙폭을 줄였다. Harvey et al., "The Impact of Volatility Targeting", JPM 45(1), 2018 — [DOI](https://doi.org/10.3905/jpm.2018.45.1.014); [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf)
- **반론**: 103개 주식 전략에서, 실시간 구현 가능한 vol-managed 버전은 대체로 원래 포트폴리오보다 확실성등가수익과 Sharpe가 낮았다. 원인은 spanning 회귀의 구조적 불안정이다. Cederburg, O'Doherty, Wang, Yan, JFE 138(1), 2020 — [DOI](https://doi.org/10.1016/j.jfineco.2020.04.015)
- HAR 계열 실현변동성 예측을 여러 자산군의 위험 관리에 적용. Bollerslev, Hood, Huss, Pedersen, "Risk Everywhere", RFS 31(7), 2018 — [DOI](https://doi.org/10.1093/rfs/hhy041)
- **손절의 가치는 수익률 과정에 달렸다**: 손절 규칙은 random walk에서는 기대수익을 낮추고, 모멘텀이나 국면 전환이 있을 때만 가치를 더한다는 분석. Kaminski & Lo, J. Financial Markets 18, 2014 [2015 이전] — [DOI](https://doi.org/10.1016/j.finmar.2013.07.001) (초록은 Crossref에 없어 기억에 근거한 요약이다. 원문 확인 권장)

### Inferences
- krx-quant는 주문을 내지 않으므로 "사이징"은 표시용 권장 비중(목표 변동성 ÷ 예측 변동성)과 신호 억제(예측 범위가 비용보다 작으면 신호 안 냄) 두 가지 형태가 된다.
- 동적 손절·익절 폭(= k × 예측 σ, ATR 배수)은 실무 관행이다. 장벽(Breiman) 폭을 Mandelbrot 분위수에 묶는 현재 방향이 이와 같다.

### Gaps
- ATR 배수 손절·익절 폭의 학술 OOS 비교는 찾지 못했다.
- 인버스 ETF(일간 리밸런싱)에 vol-targeting을 적용한 연구는 찾지 못했다.

## 6. 내재변동성 신호: VIX/VKOSPI, 기간구조, skew, 만기 효과

### Takeaway
VKOSPI(ATM IV)는 KOSPI200의 **미래 변동성**을 가장 잘 예측하는 단일 지표다(편향은 있음). 반면 IV로 **단기 방향**을 맞히는 근거는 약하다. 방향 예측력이 있는 것은 분산위험프리미엄(VRP, 분기 지평)과 개별주식 옵션 smirk(월 지평, 횡단면)뿐이고, 최근에는 약해졌다는 보고도 있다.

### Cited Findings
- VKOSPI는 약간 편향됐지만 Black–Scholes IV, RiskMetrics, GJR-GARCH보다 실현변동성을 더 잘 예측하고, 수익률 충격에 비대칭으로 반응한다. Ryu, "Implied Volatility Index of KOSPI200", Emerging Markets Finance and Trade 48, 2012 [2015 이전] — [DOI](https://doi.org/10.2753/REE1540-496X48S202)
- 과거 RV, GARCH류, 옵션 IV와 비교해 VKOSPI가 KOSPI200 현물 변동성 예측에서 전반적으로 가장 좋았다. GJR-GARCH를 더하면 설명력이 더 오를 수 있다. Chun, Cho, Ryu, Physica A 514, 2019 — [DOI](https://doi.org/10.1016/j.physa.2018.09.027)
- **VRP**(IV² − RV)는 분기 지평 주식 수익률을 예측한다. Bollerslev, Tauchen, Zhou, RFS 22(11), 2009 [2015 이전] — [DOI](https://doi.org/10.1093/rfs/hhp008). VIX²를 조건부 분산과 VRP로 나누면 VRP는 수익률을, 조건부 분산은 경기를 예측한다. Bekaert & Hoerova, J. Econometrics 183(2), 2014 [2015 이전] — [DOI](https://doi.org/10.1016/j.jeconom.2014.05.008)
- **Skew/smirk**: 개별주식 옵션 smirk가 가파른 종목은 그렇지 않은 종목보다 위험조정 연 10.9% 낮은 수익을 냈고, 예측력은 최소 6개월 간다. Xing, Zhang, Zhao, JFQA 45(3), 2010 [2015 이전] — [DOI](https://doi.org/10.1017/S0022109010000220). 2026 arXiv 프리프린트(동료심사 전)는 이 효과가 post-2020에 유의하지 않다고 보고한다 — [arXiv](https://arxiv.org/abs/2608.26115) (미검증)
- KOSPI200 수익률 예측에서는 변동성 스프레드보다 현물·선물 스프레드가 더 믿을 만한 정보였다는 국내 연구 — [earticle](https://www.earticle.net/Article/A243331) (미검증: 검색 요약에서 본 내용이며 이 링크와의 대응, 저자·연도를 확인하지 못했다)
- 만기일 효과(2절): Park & Lim 2003 — [KDI](https://kdijep.org/v.25/2/137/Expiration+Day+Effects+in+Korean+Stock+Market+Wag+the+Dog+)

### Inferences
- krx-quant의 ATM IV는 방향 피처보다 **변동폭 크기 피처**(Mandelbrot의 일간 수준, Markov의 IV 국면 배율)로 쓸 때 근거가 가장 강하다.
- 옵션 체결만으로 근월·차월 IV 기간구조(term structure)와 put−call IV 차(skew)를 만들 수 있다. 다만 이것을 4분 방향에 쓸 근거는 찾지 못했다. 쓴다면 사전 검증으로만 판단해야 한다.

### Gaps
- IV 기간구조 기울기가 KOSPI200의 일·주 단위 수익률이나 변동성을 예측한다는 KRX 실증은 찾지 못했다.
- 2010-11-11 옵션 만기 종가 급락(대량 매도 주문) 사례의 1차 출처는 확보하지 못했다.

## 7. 최근(2023–2026) ML 변동성 예측과 Optiver 대회

### Takeaway
ML은 HAR을 이기기도 하지만, 이득은 주로 추가 피처(시장 공통 변동성, 다른 종목, IV)에서 온다. 경제적 가치(volatility timing)로 평가하면 단순 선형 모형이 버틴다. Optiver 대회의 1위 핵심은 모델보다 "같은 시점의 다른 종목/비슷한 시점" 정보를 끌어오는 nearest-neighbor 피처였다.

### Cited Findings
- DJIA 종목의 RV에서 ML(정규화 회귀, 트리, NN)은 HAR 계열을 이겼다. RV 일·주·월 시차만 써도 그랬고, 지평이 길수록, 추가 피처가 있을수록 차이가 컸다. Christensen, Siggaard, Veliyev, J. Financial Econometrics 21(5), 2023 — [DOI](https://doi.org/10.1093/jjfinec/nbac020)
- 10개 지수 2000–2021에서 추가 예측변수(IV, 시차 수익률, 거시, 심리)는 일·주 지평에서 도움이 됐다. 비선형 ML은 선형보다 통계적으로 낫지 않았고, 월간 volatility-timing의 경제적 가치는 단순 모형이 더 좋았다. Branco, Rubesam, Zevallos, J. Empirical Finance 78, 2024 — [IDEAS](https://ideas.repec.org/a/eee/empfin/v78y2024ics0927539824000598.html)
- S&P 500에서 regime-switching HAR(THAR, STHAR)이 ML과 선형 모형을 일관되게 이겼고, 예측변수가 적을 때 특히 그랬다. Kilic, Federal Reserve FEDS, 2025-08 — [FRB](https://www.federalreserve.gov/econres/feds/linear-and-nonlinear-econometric-models-against-machine-learning-models.htm)
- 장중 RV 예측에서 여러 종목을 pooling하고 시장 변동성 proxy를 더하면 개선된다(intraday commonality). NN이 선형·트리보다 우세했고, 학습에 없던 종목에도 일반화됐다. Zhang, Zhang, Cucuringu, Qian, 2022 — [arXiv](https://arxiv.org/abs/2202.08962)
- **Optiver Realized Volatility Prediction(Kaggle, 2021, 10분 RV, RMSPE)**: 1위 글은 tick size로 실제 가격을 복원하고 t-SNE로 time_id 순서를 재구성했다. nearest-neighbor 집계 피처로 RMSPE를 약 0.21에서 0.19로 낮췄고, LightGBM + MLP + 1D-CNN을 혼합했다(private 0.19548) — [Kaggle 1st place 토론](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/discussion/274970); [노트북](https://www.kaggle.com/code/nyanpn/1st-place-public-2nd-place-solution?scriptVersionId=85907908). 7위도 time_id 재정렬을 썼고, 저자 스스로 실전 적용은 제한적이라 했다 — [GitHub](https://github.com/michaelpoluektov/orvp). (Kaggle 페이지는 직접 열지 못해 검색 요약 기준이다.)

### Inferences
- Optiver 1위의 이득 상당 부분은 대회 데이터 누수(시간 순서 복원)에 기대고 있다. 실전에서 옮겨 올 교훈은 "다른 종목과 시장의 동시점 변동성"을 피처로 넣는 것이다(Zhang et al.의 commonality와 같은 결론).
- krx-quant 규모(3종목 + KOSPI200 선물)에서는 거대 ML보다 HAR + 시장 피처 + 국면별 계수가 비용 대비 낫다고 본다(Branco et al., Kilic).

### Gaps
- Optiver 대회 최종 private 순위표를 직접 확인하지 못했다.
- 2025–2026 동료심사 논문 중 1분 지평 변동성 예측 ML 벤치마크는 찾지 못했다.

## 8. 실무 사용 방식과 "매분 돌릴 수 있는가"

### Takeaway
쓰임새는 네 가지다. ① 필터(변동성·국면이 나쁘면 신호를 끄거나 노출을 줄임), ② 동적 손절·익절 폭(σ 배수), ③ 사이징(목표 변동성 ÷ 예측 변동성), ④ 전략 전환(변화점이나 국면에 따라 추세/역추세). 추론 비용은 모두 매분 돌리기에 충분히 싸다. 비싼 것은 학습(MLE, Baum–Welch, JM 교차검증)뿐이고 밤 학습으로 돌리면 된다.

### Cited Findings
- 필터와 노출 축소: JM 국면으로 불리한 국면에 주식 비중을 줄임 — [Shu et al.](https://arxiv.org/abs/2402.05272). 모멘텀을 panic 상태에서 줄임 — [Daniel & Moskowitz](https://doi.org/10.1016/j.jfineco.2015.12.002)
- 사이징: 목표 변동성 스케일링 — [Harvey et al.](https://doi.org/10.3905/jpm.2018.45.1.014), [Moreira & Muir](https://doi.org/10.1111/jofi.12513). OOS 한계 — [Cederburg et al.](https://doi.org/10.1016/j.jfineco.2020.04.015)
- 전략 전환: CPD로 느린 추세와 빠른 역추세 사이를 오감 — [Wood et al.](https://arxiv.org/abs/2105.13727)
- krx-quant 측정치: LightGBM 예측은 종목당 1ms 아래이고, 알고리즘 표시 지연은 p50 0.08초, p99 약 0.14초다 — `docs/design.md`

### Inferences (1분봉 마감마다의 대략 비용, 종목당)
| 방법 | 매분 갱신 비용 | 매분 적합 |
|---|---|---|
| Parkinson/GK/RS/YZ 범위, VR, ER, BV, Lee–Mykland | 누적합으로 O(1) | 예 |
| HAR / HARQ / HAR-CJ 예측 | 내적 몇 개 | 예 (계수는 밤 학습) |
| GARCH(1,1), Realized GARCH 필터 | 재귀식 1줄 | 예 (MLE는 밤) |
| 계절 계수 × 장중 GARCH (Engle–Sokalska) | 표 조회 + 재귀 | 예 |
| HMM forward filter (K=4) | O(K²)=16 연산 | 예 (Baum–Welch는 밤) |
| JM 온라인 배정 | O(K²) | 예 (λ 교차검증은 밤) |
| BOCPD | run length 상한(예 240)으로 자르면 O(240) | 예 |
| LightGBM / 작은 MLP | 1ms 미만 | 예 |
| 고전 Hurst (R/S, 다중 창 회귀) | O(N log N), 소표본 잡음 | 가능하지만 비권장 |

### Gaps
- 위 비용표는 알고리즘 복잡도에서 추론한 것이다. krx-quant에서 직접 잰 수치는 아니다.

## krx-quant 적용 메모

### Takeaway
비용 대비 이득 순서: (1) Mandelbrot에 여러 날 계절 계수, 시장(선물)·IV 피처, 점프 분리를 넣는다. (2) Markov의 국면 깜빡임을 jump 벌점과 BOCPD로 줄인다. (3) ER·VR 기반 추세/역추세 전환을 Gauss의 조건부 재학습으로 먼저 시험한다. 모두 1분봉, 옵션 IV, 선물 데이터만으로 가능하다. 판정은 사전 검증의 상승·보합·하락 적중률로 한다.

### Inferences
**Mandelbrot(변동폭) 개선**
1. **계절 계수 교체**: "전 거래일 같은 30분 범위" 대신 최근 20–60거래일 같은 분(또는 5분 묶음)의 평균 범위로 계수를 만든다. 범위를 계수로 나눠 HAR을 돌리고 다시 곱한다(Andersen–Bollerslev 1997, Engle–Sokalska 2012). 시가 동시호가 직후, 15:20 종가 단일가 전후, 옵션 만기일(매월 둘째 목요일), 분기 동시만기일은 따로 두거나 더미로 처리한다. *데이터: 1분봉 1년, 만기 달력.*
2. **일간 수준 성분**: 곱셈 구조의 "일간 분산"에 ATM IV(VKOSPI 대용)나 전날까지의 일간 HAR을 넣는다. VKOSPI가 KOSPI200 변동성 예측에서 최상위였다(Ryu 2012; Chun et al. 2019). 종목에는 KOSPI200 대비 베타로 옮긴다. *데이터: KOSPI200 ATM 옵션 IV, 1분봉으로 만든 일간 RV.*
3. **시장·교차 종목 피처**: KOSPI200 선물의 최근 4·20분 범위와, 다른 두 종목의 범위를 회귀변수로 넣는다(intraday commonality, Optiver 1위의 neighbor 피처와 같은 아이디어). *데이터: 선물 1분봉(이미 있음).*
4. **점프 분리(HAR-CJ)**: 최근 60분 1분 중간가 수익률로 RV와 BV를 계산해 연속·점프 성분을 따로 회귀한다. Lee–Mykland 플래그도 추가한다. "큰 움직임 뒤 곧바로 잦아듦" 약점을 겨냥한다. *데이터: 1분봉.*
5. **하방 semivariance, HARQ식 보정**은 그다음 순서다(재현이 엇갈림). 평가 손실에 QLIKE를 추가한다(Patton 2011).
6. **국면별 계수(THAR식)**: IV 3분위나 Markov 국면별로 HAR 계수를 따로 둔다(Kilic 2025). 계수 수가 늘어나므로 20거래일 롤링 학습으로는 표본이 부족할 수 있다. 학습 창을 늘릴지 함께 검토한다.

**Markov(국면) 개선**
1. **지속성 강제**: Baum–Welch HMM과 나란히 statistical jump model(벌점 λ는 시계열 교차검증으로 적중률을 목표로 선택)을 도전자(challenger)로 둔다. 근거는 일간 지수 OOS(Shu et al. 2024)이므로 분 단위 효과는 검증이 필요하다. 피처는 변동성으로 정규화한 수익률(수익률 ÷ Mandelbrot σ̂), ER, VR, 선물 베이시스 변화, ΔIV.
2. **BOCPD 신호**: 표준화 1분 수익률에 BOCPD를 돌려 "최근 k분 내 변화점 확률"을 만든다. 이 값이 높으면 전이행렬을 균등 쪽으로 섞고(국면 확신 낮춤), 장벽 폭을 넓힌다. *데이터: 1분봉.*
3. **IV는 크기용으로 유지**: IV 국면은 배율(크기)에 쓰는 지금 방식이 근거와 맞다. 근월·차월 기간구조와 put−call skew는 방향 근거가 약하므로, 쓰더라도 크기·국면 피처로 시험한다. *데이터: 옵션 체결(행사가·월물별 IV).*

**추세 vs 역추세 전환(design.md의 Hurst 예비 후보)**
1. Hurst 대신 VR(q)와 ER(30–60분 창)를 쓴다. 소표본에서 R/S 편향이 크기 때문이다(Lo 1991).
2. 첫 실험은 새 알고리즘이 아니다. Gauss의 OU 되돌림을 "낮은 ER 또는 VR<1" 구간에서만 다시 학습해 κ>0이 나오는지 본다. 나오면 전환형 알고리즘으로 키운다(Wood et al.의 CPD 전환 구조 참고).
3. 일 지평에서는 KRX에서 장중 모멘텀(첫 30분 → 마지막 30분, Gao et al. 2018)이 재현되는지 시험한다. *데이터: 1분봉.*

**일·스윙 지평으로 넓힐 때**
- 일간 변동성: Yang–Zhang(밤사이 갭 포함)과 일간 HAR(1분봉으로 만든 일간 RV), 그리고 ATM IV를 쓴다.
- 방향: 시계열 모멘텀(MOP 2012)에 변동성 스케일을 더하고, panic 상태(하락 뒤 고변동)에서는 끈다(Daniel & Moskowitz). VRP(IV² − RV)는 분기 지평 근거라 스윙에는 약한 보조 피처다.
- 1년치(약 245거래일)로는 일간 국면 모형을 학습하기에 표본이 부족하다. 일간 국면은 지금처럼 매크로 일일 지표의 규칙 기반을 유지하고, 데이터가 쌓이면 JM을 시험한다.

**적중률(상승·보합·하락)과의 연결**
- 보합 띠를 고정 틱이 아니라 k × Mandelbrot σ̂로 두면 변동성 국면이 바뀌어도 세 구간의 기저율이 안정된다. 채점 정의를 바꾸는 일이므로 별도 결정이 필요하다.
- 예측 범위가 비용보다 작을 때 신호를 억제하는 필터, 그리고 국면별로 Condorcet 합의 가중을 따로 두는 것이 변동성·국면 정보를 방향 적중률로 옮기는 가장 싼 경로다.

### Gaps
- 위 항목들의 효과 크기는 모두 미지수다. 문헌 근거는 대부분 일간 데이터와 미국 시장에서 나왔다. 각 항목은 `krx-quant prevalidate`로 최근 20거래일 적중률을 확인한 뒤에만 채택해야 한다.
