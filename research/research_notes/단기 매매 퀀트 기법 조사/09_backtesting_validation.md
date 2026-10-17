# 단기 전략 백테스트·예측 검증 방법론 (과최적화 회피, up/flat/down 적중률 판정)

기준일 2026-10-17. 투자 조언이 아니라 검증 방법론 조사다.

표기:
- 모든 논문은 Crossref(DOI), arXiv API 또는 SSRN DOI(10.2139/ssrn.*)로 제목·저자·연도를 이번 조사에서 확인했다. 확인하지 못한 서지는 "서지 미확인"으로 적었다.
- ✔ = 이번 조사에서 초록이나 원문으로 내용까지 확인함.
- † = 서지는 확인했지만 해당 세부 내용은 이번에 초록을 다시 읽지 못함. 널리 알려진 내용을 기억에 따라 적은 것이므로 인용 전에 원문을 확인할 것.
- 연도 표기: 2022년 이전 결과는 "[구, YYYY]", 2023–2026 결과는 "[최신, YYYY]".

---

## 1. 과최적화와 다중검정: 여러 개를 시도했으면 기준선을 올려라

### Takeaway
여러 알고리즘·파라미터·윈도·k 값을 시도한 뒤 최고 성과를 고르면, 그 성과는 시도 횟수만큼 부풀려진다. 해법은 세 갈래다. (1) 시도 횟수 N을 기록해 통계량을 deflate하거나 문턱을 올린다(DSR, t > 3). (2) "최고 모델이 기준선보다 낫다"를 부트스트랩으로 한꺼번에 검정한다(White RC, Hansen SPA, Romano–Wolf). (3) 선택 절차 자체가 과최적화될 확률을 잰다(PBO/CSCV).

### Cited Findings
- **PBO / CSCV** [구, 2016–17] Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting", *Journal of Computational Finance*. 데이터를 S개 블록으로 나눠 가능한 모든 절반 조합을 IS/OOS로 쓰고(Combinatorially Symmetric CV), IS 최적 전략이 OOS에서 중앙값 아래로 떨어지는 빈도를 PBO로 정의한다.† — [DOI](https://doi.org/10.21314/JCF.2016.322) · [SSRN 2326253](https://papers.ssrn.com/abstract=2326253)
- **Deflated Sharpe Ratio** [구, 2014] Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality", *JPM* 40(5). 시도 횟수 N과 시도들 간 SR 분산으로 "운으로 나올 최대 SR"을 계산해 기준선으로 쓰고, 왜도·첨도·표본 길이도 보정한다. 기대 최대값 근사식은 E[max] ≈ √V[SR]·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))) (γ = Euler–Mascheroni 상수).† — [DOI](https://doi.org/10.3905/jpm.2014.40.5.094) · [SSRN 2460551](https://papers.ssrn.com/abstract=2460551)
- **Minimum Track Record Length** [구, 2012] Bailey & López de Prado, "The Sharpe Ratio Efficient Frontier": 관측된 SR이 기준 SR보다 크다고 말하려면 표본이 얼마나 길어야 하는지 계산한다.† — [SSRN 1821643](https://papers.ssrn.com/abstract=1821643)
- **SR 통계량의 표준오차** [구, 2002] Lo, "The Statistics of Sharpe Ratios", *FAJ*. 자기상관이 있으면 SR 연환산과 표준오차가 크게 달라진다.† — [DOI](https://doi.org/10.2469/faj.v58.n4.2453)
- **t > 3 문턱** [구, 2016] Harvey, Liu & Zhu, "…and the Cross-Section of Expected Returns", *RFS* 29(1). 316개 factor 문헌에 다중검정 보정(Bonferroni, Holm, BHY)을 적용해, 새 factor는 t > 3.0 정도를 넘어야 한다고 제안한다.† — [DOI](https://doi.org/10.1093/rfs/hhv059) · [SSRN 2249314](https://papers.ssrn.com/abstract=2249314)
- **Backtesting haircut** [구, 2015] Harvey & Liu, "Backtesting", *JPM* 42(1): 다중검정을 감안해 백테스트 SR에 haircut을 적용한다.† — [DOI](https://doi.org/10.3905/jpm.2015.42.1.013)
- [구, 2020] Harvey & Liu, "False (and Missed) Discoveries in Financial Economics", *JF* 75(5): Type I 오류와 Type II 오류(놓친 발견)를 함께 다룬다.† — [DOI](https://doi.org/10.1111/jofi.12951)
- **White Reality Check** [구, 2000] White, "A Reality Check for Data Snooping", *Econometrica* 68(5). 모델 우주 전체에서 기준선 대비 최고 성과가 우연인지를 bootstrap으로 검정한다.† — [DOI](https://doi.org/10.1111/1468-0262.00152)
- [구, 1999] Sullivan, Timmermann & White, "Data-Snooping, Technical Trading Rule Performance, and the Bootstrap", *JF* 54(5): RC를 기술적 매매 규칙 수천 개에 적용한 대표 사례.† — [DOI](https://doi.org/10.1111/0022-1082.00163)
- **Hansen SPA** [구, 2005] Hansen, "A Test for Superior Predictive Ability", *JBES* 23(4). 통계량을 studentize하고, 형편없는 대안 모델이 섞여도 검정력이 떨어지지 않게 RC를 고쳤다.† — [DOI](https://doi.org/10.1198/073500105000000063)
- **Romano–Wolf stepdown** [구, 2005] Romano & Wolf, "Stepwise Multiple Testing as Formalized Data Snooping", *Econometrica* 73(4). FWER을 통제하면서 "어느 모델들이" 기준선을 이기는지 단계적으로 가려낸다.† — [DOI](https://doi.org/10.1111/j.1468-0262.2005.00615.x)
- [구, 2010] Hsu, Hsu & Kuan, "Testing the predictive ability of technical analysis using a new stepwise test without data snooping bias", *J. Empirical Finance*: SPA를 stepwise로 확장했다(Step-SPA).† — [DOI](https://doi.org/10.1016/j.jempfin.2010.01.001)
- **Model Confidence Set** [구, 2011] Hansen, Lunde & Nason, *Econometrica* 79(2). "최고" 하나를 고르지 않고, 주어진 신뢰수준에서 최선과 구별되지 않는 모델 집합을 돌려준다.† — [DOI](https://doi.org/10.3982/ECTA5771)
- [구, 1995] Benjamini & Hochberg, "Controlling the False Discovery Rate", *JRSS B*: FWER 대신 FDR을 통제한다.† — [DOI](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x)
- [구, 1994] Politis & Romano, "The Stationary Bootstrap", *JASA*. RC와 SPA가 쓰는 시계열 bootstrap이다.† — [DOI](https://doi.org/10.1080/01621459.1994.10476870)
- **유효 시도 횟수** [구, 2019] López de Prado, "A Data Science Solution to the Multiple-Testing Crisis in Financial Research", *JFDS* 1(1): 서로 비슷한 시도들을 clustering해 "유효 N"을 추정하고, 이를 DSR에 넣는다.† — [DOI](https://doi.org/10.3905/jfds.2019.1.099) · [SSRN 3177057](https://papers.ssrn.com/abstract=3177057); 관련: López de Prado & Lewis, "Detection of False Investment Strategies Using Unsupervised Learning Methods"† — [SSRN 3167017](https://papers.ssrn.com/abstract=3167017)
- **Backtesting protocol** [구, 2019] Arnott, Harvey & Markowitz, "A Backtesting Protocol in the Era of Machine Learning", *JFDS* 1(1). 체크리스트 형식이다. 사전 가설(경제적 동기), 시도 횟수 기록과 다중검정 보정, 표본·데이터 선택의 근거, 교차검증, 모델 복잡도 통제, 연구 문화(실패 결과도 기록)를 다룬다.† — [DOI](https://doi.org/10.3905/jfds.2019.1.064) · [SSRN 3275654](https://papers.ssrn.com/abstract=3275654)
- [최신, 2025] López de Prado, Lipton & Zoonekynd, "How to Use the Sharpe Ratio" (SSRN, 2025-10 게재, 2026-03 개정). SR은 추론 체계와 보고 규율을 갖출 때만 쓸모 있다고 주장하고, 다중검정 보정을 핵심 문제 중 하나로 다룬다. 재현 코드가 공개되어 있다.✔(검색 요약 기준) — [SSRN 5520741](https://papers.ssrn.com/abstract=5520741)

### Inferences
- 같은 원리를 Sharpe 대신 **log loss 개선량의 DM t-통계량**에도 적용할 수 있다. 독립인 시도 N개의 최대 t는 영가설에서 평균 약 1.87(N=20), 약 2.51(N=100)이다(표준정규 최대값 기대치). 알고리즘 20개를 시험하고 최고만 보면 "t ≈ 1.9"는 우연 수준이다.
- 여기서 N은 레지스트리에 남은 알고리즘 수가 아니다. 리플레이에서 시험한 모든 변형(k, 윈도 20/40/60일, 하이퍼파라미터, 탈락한 알고리즘 포함)의 수다. 탈락 기록이 없으면 deflate할 수 없다.

### Gaps
- DSR·PBO는 수익률(SR) 기준으로 만들어졌다. log loss나 다범주 적중률에 바로 쓰는 "deflated log loss" 표준 공식은 찾지 못했다. 위 Inference처럼 최대 t의 분포로 근사하거나 SPA/Romano–Wolf bootstrap을 쓰는 것이 현실적이다.

---

## 2. 게재 후 감쇠와 재현성: 백테스트 성과는 실전에서 줄어든다

### Takeaway
학술 예측변수의 수익은 표본 밖에서 약 1/4, 게재 후 약 절반 넘게 줄어든다. 수백 개 anomaly 중 상당수는 엄격한 기준에서 재현되지 않는다. 다만 Bayesian 재평가로는 "대부분 재현된다"는 반론도 있다. 실무 교훈은 "백테스트 성과에 haircut을 미리 적용하라"이다.

### Cited Findings
- [구, 2016] McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?", *JF* 71(1). 예측변수 97개를 분석했다. 포트폴리오 수익이 표본 밖에서 26%, 게재 후 58% 낮았다. 26%는 data mining 효과의 상한이고, 게재 정보에 따른 매매 효과는 약 32%로 추정했다. 표본 내 수익이 높았던 예측변수일수록 게재 후 감소가 컸다.✔ — [DOI](https://doi.org/10.1111/jofi.12365)
- [구, 2020] Hou, Xue & Zhang, "Replicating Anomalies", *RFS* 33(5). NYSE breakpoint와 가치가중으로 microcap 영향을 줄이자, anomaly 452개 중 65%가 |t| ≥ 1.96을 넘지 못했다. 다중검정 문턱 2.78을 적용하면 실패율은 82%였다. 재현된 것도 경제적 크기가 원 논문보다 훨씬 작았다. 특히 "trading frictions" 범주는 96%가 실패했다.✔ — [DOI](https://doi.org/10.1093/rfs/hhy131)
- [최신, 2023] Jensen, Kelly & Pedersen, "Is There a Replication Crisis in Finance?", *JF* 78(5). Bayesian 재현 모형으로 보면 factor 대다수가 재현되고, 13개 테마로 묶이며, 93개국 새 데이터에서도 작동한다. 관측된 factor 수가 많을수록 증거가 약해지지 않고 오히려 강해진다고 주장한다.✔ — [DOI](https://doi.org/10.1111/jofi.13249)
- [구, 2021] Chen, "Most Claimed Statistical Findings in Cross-Sectional Return Predictability Are Likely True" (SSRN). 다중검정 비관론에 대한 반론이다.† (학술지 게재 여부 미확인) — [SSRN 3912915](https://papers.ssrn.com/abstract=3912915)

### Inferences
- 이 문헌들은 월·일 단위 cross-section anomaly를 다룬다. 4분 단위 미시구조 신호에 감쇠율을 그대로 옮길 수는 없다. 그래도 "선택된 최고 모델의 표본 내 성과는 체계적으로 과대평가된다"는 방향은 같다. HXZ에서 trading frictions 범주 실패율이 96%였다는 점은 유동성·미시구조 계열 신호가 특히 취약할 수 있음을 시사한다.

### Gaps
- 초단기(분 단위) 신호의 게재 후 감쇠를 정량화한 학술 연구는 찾지 못했다.

---

## 3. 데이터 분할: walk-forward, purged/embargoed k-fold, CPCV

### Takeaway
금융 시계열에서 무작위 k-fold는 label이 겹쳐 정보가 샌다. 기본은 시간 순서를 지키는 walk-forward다. CV를 쓰려면 테스트 구간과 label 기간이 겹치는 학습 표본을 지우고(purge), 테스트 직후 구간을 비워야 한다(embargo). 경로를 여러 개 만들어 성과 분포를 보려면 CPCV를 쓴다. 롤링 윈도 길이를 결과를 보고 고르면 그 자체가 data snooping이다.

### Cited Findings
- [구, 2018] López de Prado, *Advances in Financial Machine Learning* (Wiley). 7장 purged k-fold와 embargo, 12장 Combinatorial Purged CV(CPCV), 3장 triple-barrier labeling을 다룬다. 겹치는 label이 서로 독립이 아니라는 점(concurrency와 sample uniqueness)도 설명한다.† — [Wiley](https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086) (URL 서지 미확인, ISBN 978-1-119-48208-6) · 요약판 [SSRN 3104816 "The 10 Reasons Most Machine Learning Funds Fail"](https://papers.ssrn.com/abstract=3104816)
- [최신, 2024] Arian, Norouzi Mobarekeh & Seco, "Backtest overfitting in the machine learning era: A comparison of out-of-sample testing methods in a synthetic controlled environment", *Knowledge-Based Systems*. 합성 데이터에서 walk-forward, k-fold, purged k-fold, CPCV를 비교했다. CPCV가 PBO가 낮고 DSR 통계량이 좋아 과최적화 억제에 우수했다. walk-forward는 false discovery 방지가 약하고 시점에 따른 변동이 컸다. Bagged CPCV와 Adaptive CPCV 변형도 제안한다.† — [DOI](https://doi.org/10.1016/j.knosys.2024.112477) · [SSRN 4686376](https://papers.ssrn.com/abstract=4686376)
- [구, 2018] Bergmeir, Hyndman & Koo, "A note on the validity of cross-validation for evaluating autoregressive time series prediction", *CSDA* 120. 순수 AR 모형이고 오차가 상관되지 않으면 표준 k-fold CV도 유효하다.† — [DOI](https://doi.org/10.1016/j.csda.2017.11.003)
- [구, 2020] Cerqueira, Torgo & Mozetič, "Evaluating time series forecasting models: an empirical study on performance estimation methods", *Machine Learning* 109. 실제 비정상(non-stationary) 시계열에서는 순서를 지키는 holdout 계열(반복 holdout 등)이 더 정확했다. 정상 시계열에서는 CV 계열도 괜찮았다.† — [DOI](https://doi.org/10.1007/s10994-020-05910-7) · [arXiv 1905.11744](https://arxiv.org/abs/1905.11744)
- **윈도 선택** [구, 2007] Pesaran & Timmermann, "Selection of estimation window in the presence of breaks", *J. Econometrics* 137. 구조변화가 있으면 최적 추정 윈도가 bias–variance trade-off로 정해진다. 여러 윈도 예측을 결합하는 방법도 제안한다.† — [DOI](https://doi.org/10.1016/j.jeconom.2006.03.010)
- [구, 2012] Rossi & Inoue, "Out-of-Sample Forecast Tests Robust to the Choice of Window Size", *JBES* 30(3). 윈도 크기를 바꿔 가며 OOS 검정을 하고 가장 좋은 것을 보고하면 size가 왜곡된다. 여러 윈도에 걸쳐 robust한 검정 통계량을 제안한다.† — [DOI](https://doi.org/10.1080/07350015.2012.693850)
- **Regime 의존성** [구, 2006] Christoffersen & Diebold, "Financial Asset Returns, Direction-of-Change Forecasting, and Volatility Dynamics", *Management Science* 52(8). 기대수익이 0이 아니면 변동성 의존성만으로도 부호(방향) 의존성이 생긴다. 평균 예측력 없이도 방향 예측력이 있을 수 있다. 방향 의존성은 비선형이라 부호 자기상관이나 runs test로는 잘 잡히지 않는다.✔ — [DOI](https://doi.org/10.1287/mnsc.1060.0520)

### Inferences
- krx-quant처럼 "밤마다 직전 거래일까지로 재학습하고 다음 날 예측"하는 구조는 그 자체로 walk-forward다. 장 마감으로 label이 끊기므로 일 단위 분할에서는 purge가 거의 필요 없다. 그러나 장중 분할(예: 오전 학습, 오후 검증)이나 4분 label이 겹치는 연속 예측을 CV할 때는 최소 4분 purge와 embargo가 필요하다.
- touch target(±k틱 도달)은 사실상 변동성 사건이다. Christoffersen–Diebold의 결과로 보면 up/down 적중률의 상당 부분은 "변동성이 큰 시간대·국면을 맞힌 것"일 수 있다. 그래서 regime별(변동성 3분위, 시간대, 장 시작·마감 근처) 성과를 따로 봐야 진짜 방향성 skill과 변동성 skill을 구분할 수 있다.
- 20–60일 롤링 윈도 중 하나를 사후 성과로 고르면 Rossi–Inoue가 경고하는 window snooping이 된다. 윈도 후보 수도 1절의 시도 횟수 N에 넣어야 한다.

### Gaps
- 분 단위 LOB 예측에서 CPCV와 walk-forward를 실데이터로 비교한 연구는 찾지 못했다. Arian et al.은 합성 데이터다.

---

## 4. 예측 평가: 기준선 대비 비교, 방향성 검정, 확률 점수, flat 우세 문제

### Takeaway
(1) 두 예측기 비교는 손실 차이의 시계열에 대한 DM 검정(소표본 HLN 보정, 겹치는 horizon이면 HAC)으로 한다. 롤링 윈도 재학습이면 Giacomini–White가 더 맞다. (2) 방향 적중률이 우연 이상인지는 Pesaran–Timmermann 검정으로 본다. 3범주(up/flat/down)이고 자기상관이 있으면 PT(2009) 다범주 버전을 쓴다. (3) 확률 예측은 strictly proper score(log loss, Brier, ordinal이면 RPS)와 calibration으로 본다. (4) flat이 우세하면 전체 accuracy는 의미가 없다. 클래스별 precision을 base rate와 비교하고, balanced accuracy, Cohen's kappa, Gerrity/Heidke 같은 "equitable" skill score를 쓴다.

### Cited Findings
- **Diebold–Mariano** [구, 1995] Diebold & Mariano, "Comparing Predictive Accuracy", *JBES* 13(3). 임의의 손실함수에서 두 예측의 손실 차이 d_t의 평균이 0인지 검정한다. h-step 예측이면 d_t에 MA(h−1) 구조가 있으므로 장기분산(HAC)을 쓴다.† — [DOI](https://doi.org/10.1080/07350015.1995.10524599)
- [구, 1997] Harvey, Leybourne & Newbold, "Testing the equality of prediction mean squared errors", *IJF* 13(2). DM의 소표본 보정과 t 분포 기준값을 제시한다.† — [DOI](https://doi.org/10.1016/S0169-2070(96)00719-4)
- [구, 2015] Diebold, "Comparing Predictive Accuracy, Twenty Years Later: A Personal Perspective on the Use and Abuse of Diebold–Mariano Tests", *JBES* 33(1). DM은 "예측"을 비교하는 도구이고 "모델"을 비교하는 도구가 아니라고 정리한다. 진짜 OOS 예측을 비교할 때 쓰라는 것이다.† — [DOI](https://doi.org/10.1080/07350015.2014.983236)
- **Giacomini–White** [구, 2006] "Tests of Conditional Predictive Ability", *Econometrica* 74(6). 추정 윈도가 유한한 롤링 방식을 포함해 "예측 방법"(모델, 추정 윈도, 추정법을 묶은 것)을 비교한다. 조건부(어떤 상황에서 더 나은지) 검정도 된다.† — [DOI](https://doi.org/10.1111/j.1468-0262.2006.00718.x)
- [구, 2007] Clark & West, "Approximately normal tests for equal predictive accuracy in nested models", *J. Econometrics* 138. 기준선이 큰 모델에 포함되는 nested 경우에 쓰는 보정이다.† — [DOI](https://doi.org/10.1016/j.jeconom.2006.05.023)
- **Pesaran–Timmermann** [구, 1992] "A Simple Nonparametric Test of Predictive Performance", *JBES* 10(4). 예측 부호와 실현 부호가 독립인지 검정한다. 적중률을 "양쪽 주변분포만으로 기대되는 적중률"과 비교하므로 base rate를 자동으로 반영한다.† — [DOI](https://doi.org/10.1080/07350015.1992.10509922)
- [구, 2009] Pesaran & Timmermann, "Testing Dependence Among Serially Correlated Multicategory Variables", *JASA* 104(485). PT 검정을 다범주와 serial correlation이 있는 경우로 확장했다. up/flat/down 3범주에 직접 맞는다.† — [DOI](https://doi.org/10.1198/jasa.2009.0113)
- [구, 2005] Anatolyev & Gerko, "A Trading Approach to Testing for Predictability", *JBES* 23(4). 방향 예측대로 매매했을 때의 초과수익이 0인지 보는 Excess Profitability 검정이다.† — [DOI](https://doi.org/10.1198/073500104000000640)
- [구, 2011] Blaskowitz & Herwartz, "On economic evaluation of directional forecasts", *IJF* 27(4). 방향 예측을 적중률이 아니라 경제적 가치로 평가한다.† — [DOI](https://doi.org/10.1016/j.ijforecast.2010.07.002)
- **Proper scoring rules** [구, 2007] Gneiting & Raftery, "Strictly Proper Scoring Rules, Prediction, and Estimation", *JASA* 102(477). log score와 Brier(quadratic) score는 strictly proper라서 정직한 확률 보고가 최적이다.† — [DOI](https://doi.org/10.1198/016214506000001437). Brier score 원전은 Brier (1950), *Monthly Weather Review* 78(1)이다(서지 미확인, URL 생략).
- **RPS (순서형 범주)** [구, 1969] Epstein, "A Scoring System for Probability Forecasts of Ranked Categories", *J. Applied Meteorology*. 범주에 순서가 있으면 누적확률로 채점한다. 실제가 down인데 up을 예측하면 flat을 예측했을 때보다 더 벌한다.† — [DOI](https://doi.org/10.1175/1520-0450%281969%29008%3C0985:ASSFPF%3E2.0.CO%3B2)
- **Equitable skill score** [구, 1992] Gandin & Murphy, "Equitable Skill Scores for Categorical Forecasts", *Monthly Weather Review*; Gerrity, "A Note on Gandin and Murphy's Equitable Skill Score". 다범주에서 무작위 예측과 상수 예측(예: 항상 flat)이 모두 0점을 받도록 설계한 점수(Gerrity score)다.† — [Gandin & Murphy DOI](https://doi.org/10.1175/1520-0493%281992%29120%3C0361:ESSFCF%3E2.0.CO%3B2) · [Gerrity DOI](https://doi.org/10.1175/1520-0493%281992%29120%3C2709:ANOGAM%3E2.0.CO%3B2)
- **Calibration** [구, 2017] Guo, Pleiss, Sun & Weinberger, "On Calibration of Modern Neural Networks", ICML. 현대 신경망은 과신(overconfident)하는 경향이 있다. reliability diagram, ECE, temperature scaling을 제시한다.† — [arXiv 1706.04599](https://arxiv.org/abs/1706.04599)
- [구, 1960] Cohen, "A Coefficient of Agreement for Nominal Scales": kappa = (관측 일치 − 우연 일치)/(1 − 우연 일치).† — [DOI](https://doi.org/10.1177/001316446002000104)
- [구, 2010] Brodersen et al., "The Balanced Accuracy and Its Posterior Distribution", ICPR. 클래스별 recall의 평균으로, 불균형 데이터에서 다수 클래스만 맞히는 분류기를 걸러낸다.† — [DOI](https://doi.org/10.1109/ICPR.2010.764)
- **LOB 3범주 예측의 함정** [최신, 2024] Prata et al., "LOB-based deep learning models for stock price trend prediction: a benchmark study", *Artificial Intelligence Review*. LOB 기반 SOTA 딥러닝 15개를 LOBCAST로 재평가했다. 모든 모델이 새 데이터에서 성능이 크게 떨어졌다.✔ — [DOI](https://doi.org/10.1007/s10462-024-10715-4) · [arXiv 2308.01915](https://arxiv.org/abs/2308.01915)
- [최신, 2025] Briola, Bartolucci & Aste, "Deep limit order book forecasting: a microstructural guide", *Quantitative Finance* 25(7). 종목의 미시구조 특성이 딥러닝 효용을 좌우한다. 높은 예측력이 곧 실행 가능한 매매 신호는 아니다. 전통적 ML 지표는 LOB 예측의 질을 제대로 평가하지 못한다고 보고, "완결된 거래를 맞힐 확률" 기반의 평가틀을 제안한다.✔ — [DOI](https://doi.org/10.1080/14697688.2025.2522911) · [arXiv 2403.09267](https://arxiv.org/abs/2403.09267)
- [최신, 2023] Kolm, Turiel & Westray, "Deep order flow imbalance: Extracting alpha at multiple horizons from the limit order book", *Mathematical Finance*. Nasdaq 115종목에서 order book 원자료보다 order flow(정상화한 입력)로 학습한 모델이 더 정확했다. "정보가 풍부한" 종목일수록 예측이 잘 됐다. 유효 horizon은 평균 가격변화 약 2회 정도였다.✔ — [DOI](https://doi.org/10.1111/mafi.12413)
- [구, 2018–19] Zhang, Zohren & Roberts, "DeepLOB". 임계값으로 up/stationary/down 3범주 label을 만드는 표준 설정이다.† — [arXiv 1808.03668](https://arxiv.org/abs/1808.03668)

### Inferences
- **base rate 대비 판정은 클래스별로 해야 한다.** "up이라고 말했을 때 실제 up 비율"(up precision)을 무조건부 P(up)과 비교한다. 예측 분포가 같은 무작위 예측기의 기대 적중률은 Σ_c P(pred=c)·P(actual=c)다. PT 검정의 영가설이 바로 이 값이다.
- **always-flat이 accuracy 1등이 되는 문제:** Gerrity score와 Cohen's kappa는 always-flat에 0점을 준다. balanced accuracy는 always-flat에 1/3을 준다. 리더보드에 이 중 하나를 추가하면 "flat만 많이 맞힌 모델"이 위로 오지 않는다.
- **표본 크기 감각(산술):** 독립 표본 n에서 적중률(p≈0.5)의 표준오차는 √(0.25/n)이다. 독립 up 신호 1,000개면 약 1.6%p이고, t > 3을 넘으려면 base rate보다 약 4.7%p 높아야 한다. 4분 label을 1분마다 만들면 인접 예측이 겹쳐 유효 표본은 대략 1/4로 준다. HAC lag는 최소 (겹치는 예측 수 − 1)이어야 한다.
- log loss는 확률의 질 전체를 본다. 반면 사용자가 판단하는 "클래스별 적중률"은 argmax 결정만 본다. 둘이 엇갈리는 모델(log loss는 좋은데 up/down을 거의 말하지 않는 모델)이 흔하므로 두 축을 모두 리더보드에 둬야 한다.

### Gaps
- PT(2009) 다범주 검정과 Gerrity score를 고빈도 LOB 3범주 예측에 적용한 실증 연구는 찾지 못했다.
- Briola et al.의 "완결된 거래 확률" 지표의 정확한 정의는 이번에 원문까지 확인하지 못했다.

---

## 5. 단기 비용 모형: spread, 체결 확률, 거래세, 손익분기 적중률

### Takeaway
4분 horizon에서는 비용이 신호를 거의 다 먹는다. aggressive 주문은 spread를 확실히 지불한다. passive 주문은 spread를 아끼지만 체결 확률이 1보다 작고, 체결된 주문은 불리한 쪽으로 선택된다(adverse selection). 한국 주식은 2026년부터 매도 시 거래세가 0.20%(KOSPI는 거래세 0.05%와 농특세 0.15%, KOSDAQ은 0.20%)다. 1틱이 가격의 0.1–0.2% 수준인 종목이면 세금만으로 1–2틱이다.

### Cited Findings
- **체결 확률·큐 위치** [구, 2010] Cont, Stoikov & Talreja, "A Stochastic Model for Order Book Dynamics", *Operations Research* 58(3). LOB를 큐 모형으로 보고, mid 가격이 움직이기 전에 지정가 주문이 체결될 확률 같은 조건부 확률을 계산한다.† — [DOI](https://doi.org/10.1287/opre.1090.0780)
- [구, 2015] Huang, Lehalle & Rosenbaum, "Simulating and Analyzing Order Book Data: The Queue-Reactive Model", *JASA*. 큐 상태에 반응하는 주문 흐름 모형으로, 체결 시뮬레이션에 쓴다.† — [DOI](https://doi.org/10.1080/01621459.2014.982278)
- [구, 2016] Moallemi & Yuan, "A Model for Queue Position Valuation in a Limit Order Book" (SSRN). 큐 앞자리의 가치를 체결 확률과 adverse selection 두 요소로 분해한다.† — [SSRN DOI](https://doi.org/10.2139/ssrn.2996221)
- [최신, 2024] Arroyo, Cartea, Moreno-Pino & Zohren, "Deep attentive survival analysis in limit order books: estimating fill probabilities with convolutional-transformers", *Quantitative Finance*. passive와 aggressive의 선택에서 핵심은 passive 주문의 체결 확률이다. LOB 상태에서 체결시간 분포를 survival 모형으로 추정하고, proper scoring rule로 비교한다.✔ — [DOI](https://doi.org/10.1080/14697688.2023.2286351) · [arXiv 2306.05479](https://arxiv.org/abs/2306.05479)
- [최신, 2024] Lokin & Yu, "Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows" (arXiv).† — [arXiv 2403.02572](https://arxiv.org/abs/2403.02572)
- [최신, 2025] Zhong, Bacry, Guilloux & Muzy, "KANFormer for Predicting Fill Probabilities via Survival Analysis in Limit Order Books" (arXiv, 2025-12).† — [arXiv 2512.05734](https://arxiv.org/abs/2512.05734)
- **한국 증권거래세 (2026)** 2026-01-01 이후 양도분부터 KOSPI는 증권거래세 0.05%와 농어촌특별세 0.15%를 합쳐 0.20%(2025년 0.15%), KOSDAQ은 0.20%(2025년 0.15%)다. 금투세 폐지에 따라 시행령 개정으로 되돌린 것이다.✔ — [세정신문 2025-12-01(개정안 보도)](https://www.taxtimes.co.kr/news/article.html?no=272624) · [세계일보 2026-01-05(시행 보도)](https://www.segye.com/newsView/20260105516769). 시행령 공포일은 국가법령정보센터에서 직접 확인하지 못했다.
- [구, 2018] López de Prado, AFML 3장 triple-barrier method: 상단 목표, 하단 손절, 시간 만료 세 장벽 가운데 먼저 닿는 것으로 label을 붙인다. krx-quant의 touch target과 같은 계열이다.† — [SSRN 3104816](https://papers.ssrn.com/abstract=3104816)

### Inferences
- **손익분기 적중률(산술 유도, 출처 불필요):** 신호가 up일 때 매수한다고 하자. +u틱 도달 시 이익 u, −d틱 도달 시 손실 d, 둘 다 안 닿으면(flat) 약 0이고, 왕복비용이 C틱이다. 이때 E = P(up|신호)·u − P(down|신호)·d − C ≥ 0이어야 한다. 대칭(u=d=k)이면 **P(up|신호) − P(down|신호) ≥ C/k**다. flat이 섞이면 필요한 적중률은 더 오른다. flat 결과가 없는 이진 대칭 장벽이면 p* = 1/2 + C/(2k)다.
- **수치 예시(가정 포함):** 주가 70,000원, 호가단위 100원(가정, KRX 호가단위표는 이번에 확인하지 않음)이면 매도 거래세 0.20% = 140원 = 1.4틱이다. aggressive 진입 시 spread 1틱, 증권사 수수료(가정, 수 bp)를 더하면 C ≈ 2.5틱이다. k=1이면 P(up)−P(down) ≥ 2.5라 불가능하고, k=3이어도 ≥ 0.83이다. 4분 horizon에서 개별 주식을 직접 매매하면 적중률이 아무리 좋아도 수익화가 사실상 어렵다.
- touch는 체결이 아니다. "best bid가 +k틱에 닿았다"는 것이 내 매도 지정가가 체결됐다는 뜻은 아니다. 큐 뒤에 있으면 가격이 닿고도 되돌아갈 수 있다. 반대로 진입을 passive로 하면, 체결되는 경우가 주로 가격이 나에게 불리하게 움직일 때다(adverse selection). 이 비대칭 때문에 적중률이 같아도 실현 성과가 낮아진다.
- 인버스 ETF로 down 신호를 쓸 때의 비용 구조(국내 ETF 매도분 증권거래세 면제 여부, ETF 호가단위, LP 호가 spread)는 개별 주식과 다르다. ETF 거래세 면제는 이번 조사에서 확인하지 못했다(미확인).

### Gaps
- KRX 거래소·청산 수수료율, 2026년 KRX 호가단위표, 국내 ETF 거래세 면제 여부는 이번 조사에서 1차 출처로 확인하지 못했다.
- 한국 시장에서 큐 위치·체결 확률을 실증한 논문은 찾지 못했다.

---

## 6. 흔한 편향: look-ahead, survivorship, 파라미터 탐색, Kaggle의 누수

### Takeaway
가장 흔한 누수는 시점 정렬 오류다. 결정 시각 t에 아직 알 수 없는 값을 feature로 쓰는 경우로, bar의 close를 그 close 시각의 결정에 쓰거나 전체 기간으로 정규화하는 것이 예다. 다음은 종목 선택 편향(지금 유동성이 좋은 종목만 고름)과 검증 세트 재사용(리플레이 결과를 보고 고치고 다시 리플레이)이다. Kaggle 금융 대회는 익명화된 데이터에서도 시간 순서를 역공학해 누수를 만들 수 있음을 보여 줬다.

### Cited Findings
- [구, 2012] Kaufman, Rosset, Perlich & Stitelman, "Leakage in data mining: formulation, detection, and avoidance", *ACM TKDD*. 누수의 형식적 정의와 탐지·회피 방법을 다룬다. 핵심 원칙은 "예측 시점에 정당하게 알 수 있던 정보만 쓴다"이다.† — [DOI](https://doi.org/10.1145/2382577.2382579)
- [구, 1992] Brown, Goetzmann, Ibbotson & Ross, "Survivorship Bias in Performance Studies", *RFS* 5(4). 살아남은 표본만 보면 성과와 그 지속성이 부풀려진다.† — [DOI](https://doi.org/10.1093/rfs/5.4.553)
- [구, 1999] Sullivan, Timmermann & White: 같은 데이터에서 규칙 우주를 탐색하면 data snooping이 생긴다(1절).† — [DOI](https://doi.org/10.1111/0022-1082.00163)
- **Kaggle Optiver Realized Volatility Prediction (2021)** 7위 솔루션 README에 따르면, 정규화된 가격과 tick size로 실제 가격을 복원하고 익명화된 `time_id`의 시간 순서를 역공학해 재정렬했다. 작성자 스스로 리더보드 점수는 높아도 실제 응용 가치는 제한적이라고 적었다.✔ — [GitHub michaelpoluektov/orvp](https://github.com/michaelpoluektov/orvp)
- Jane Street Market Prediction 등 다른 Kaggle 대회의 상위 솔루션이 purged group time-series split을 썼다는 정황은 2차 자료(라이브러리 문서, 블로그)뿐이었다. 1차 솔루션 글은 확인하지 못했다(Gaps 참조).
- [구, 2019] Arnott, Harvey & Markowitz 프로토콜: 결과를 본 뒤 가설을 바꾸지 말고, 시도한 모든 변형을 기록하라고 권한다(1절).† — [DOI](https://doi.org/10.3905/jfds.2019.1.064)

### Inferences
- krx-quant 맥락의 체크포인트:
  - **시점 정렬:** 예측 시각 t의 feature는 거래소 타임스탬프가 아니라 "엔진이 실제로 받은 시각" 기준으로 t 이전이어야 한다. touch label 구간 [t, t+4분]의 첫 호가가 feature에 들어가면 누수다. 1분 bar를 쓴다면 bar close 값은 그 bar가 끝난 뒤에야 쓸 수 있다.
  - **정규화 통계와 앙상블 가중치:** 롤링 윈도 학습 데이터로만 계산해야 한다. 앙상블의 log-loss 가중치도 과거 성과로만 정하고, 채점 당일 성과로 정하면 안 된다. 그러지 않으면 앙상블 점수가 in-sample이 된다.
  - **종목 선택 편향:** 3종목을 "지금 유동성·관심이 높아서" 골랐다면 과거 성과도 그 선택에 조건부다. 결론을 일반화할 때 주의한다.
  - **리플레이 세트 재사용:** 20일 리플레이로 탈락시키고, 고치고, 같은 20일로 다시 통과시키면 그 20일은 더 이상 OOS가 아니다(adaptive overfitting). 통과 뒤 새 날짜에서 성과를 따로 기록해야 한다.

### Gaps
- Jane Street(2020–21, 2024–25), Optiver Trading at the Close(2023) 등의 1위 솔루션 원문은 Kaggle 토론 페이지 접근 문제로 확인하지 못했다.

---

## 7. 최근 연구 (2023–2026) 요약

### Takeaway
최근 흐름은 셋이다. (1) 재현성 논쟁은 비관론(HXZ)과 Bayesian 낙관론(JKP 2023)으로 나뉜다. (2) 검증 방법 비교에서는 CPCV가 walk-forward보다 과최적화 억제에 낫다는 합성 실험(2024)이 있다. (3) LOB 딥러닝 예측은 "벤치마크 정확도가 새 데이터와 실행 가능성으로 이어지지 않는다"는 재평가(2024–25)가 나왔다. 체결 확률 모형은 survival analysis로 옮겨가고 있다.

### Cited Findings
- Jensen, Kelly & Pedersen (2023), *JF*: factor 대다수가 재현된다(2절).✔ — [DOI](https://doi.org/10.1111/jofi.13249)
- Arian et al. (2024), *KBS*: CPCV가 walk-forward보다 PBO와 DSR에서 우수하다(3절).† — [DOI](https://doi.org/10.1016/j.knosys.2024.112477)
- Prata et al. (2024), *AI Review*: LOB 딥러닝 15개 모두 새 데이터에서 성능이 크게 떨어졌다.✔ — [DOI](https://doi.org/10.1007/s10462-024-10715-4)
- Briola, Bartolucci & Aste (2025), *QF*: 예측력이 실행 가능한 신호와 같지 않고, ML 지표로는 LOB 예측을 평가하기 부족하다.✔ — [DOI](https://doi.org/10.1080/14697688.2025.2522911)
- Kolm, Turiel & Westray (2023), *Math. Finance*: order flow 입력이 우수하고, 유효 horizon은 가격변화 약 2회다.✔ — [DOI](https://doi.org/10.1111/mafi.12413)
- Arroyo et al. (2024), *QF*; Lokin & Yu (2024); Zhong et al. (2025): 체결 확률을 survival analysis로 추정한다(5절).✔/† — [DOI](https://doi.org/10.1080/14697688.2023.2286351) · [arXiv 2403.02572](https://arxiv.org/abs/2403.02572) · [arXiv 2512.05734](https://arxiv.org/abs/2512.05734)
- López de Prado, Lipton & Zoonekynd (2025–26 개정), SSRN: SR 추론, 다중검정, 보고 규율을 다룬다(1절).✔(검색 요약) — [SSRN 5520741](https://papers.ssrn.com/abstract=5520741)

### Inferences
- Kolm et al.의 "유효 horizon ≈ 가격변화 2회"는 Nasdaq 대형주 기준이다. krx-quant의 4분과 ±k틱 사이의 관계는 종목별 틱 변화 빈도로 환산해 봐야 한다. 4분 동안 평균 틱 변화가 많으면 4분은 "유효 horizon 밖"일 수 있다.

### Gaps
- 2026년에 나온 백테스트 과최적화 관련 주요 학술지 논문은 이번 검색에서 따로 찾지 못했다.

---

## krx-quant 적용 메모

엔진의 자동 채점과 20일 리플레이 사전검증에 붙일 수 있는 구체적 검사다. 사용자 기준인 "클래스별 적중률 우선"에 맞춰 순서를 정했다.

1. **클래스별 적중률을 base rate와 비교하는 검정**
   - 각 알고리즘의 up precision과 down precision을 같은 기간의 무조건부 P(up), P(down)과 나란히 보여 준다. 차이의 신뢰구간은 겹치는 label을 감안해 일 단위 block bootstrap으로 낸다.
   - **Pesaran–Timmermann (2009) 다범주 검정**으로 "예측 범주와 실제 범주가 독립"인지 검정하고 p값을 리더보드 열로 추가한다(1992년 이진 버전은 up 대 not-up, down 대 not-down에 각각 쓴다).
2. **always-flat에 0점을 주는 skill 점수:** Gerrity score(또는 Heidke), Cohen's kappa, balanced accuracy를 리더보드에 추가한다. accuracy와 log loss만으로는 "flat만 많이 말하는 모델"이 위로 올 수 있다.
3. **기준선 대비 DM 검정:** 4개 기준선(always-flat, previous window, base-rate random, 시간대 climatology) 각각에 대해 예측별 log loss 차이 d_t를 만든다. HAC 분산(lag ≥ 겹침 수 − 1, 또는 일 단위 block)과 HLN 보정을 쓴 DM t값을 기록한다. 롤링 재학습이므로 해석은 Giacomini–White식 "예측 방법 비교"로 한다. 가장 강한 기준선(대개 climatology)을 이기는지가 핵심이다.
4. **시도 횟수 기록과 deflation:** 리플레이에 들어간 모든 변형(탈락 포함, k, 윈도, 하이퍼파라미터)을 레지스트리에 누적한다. 사전검증 통과 문턱을 고정 t=2가 아니라 시도 수 N에 따라 올린다. 참고로 영가설에서 기대 최대 t는 N=20일 때 약 1.9, N=100일 때 약 2.5이고, Harvey–Liu–Zhu 식 고정 문턱은 t > 3이다. 여러 알고리즘을 한꺼번에 볼 때는 Hansen SPA나 Romano–Wolf stepdown(stationary/일 block bootstrap)으로 "기준선을 이기는 알고리즘 집합"을 고른다. Model Confidence Set으로 "최고와 구별 안 되는 집합"을 표시해도 된다.
5. **리플레이의 재사용 금지와 shadow 기간:** 20일 리플레이를 통과한 뒤 앙상블에 편입하기 전에, 새 날짜에서 shadow로 채점한 성과를 따로 쌓는다. 같은 20일로 재시험을 반복했다면 그 횟수를 4번의 N에 넣는다.
6. **윈도 선택 snooping 방지:** 20/40/60일 윈도를 사후 성과로 고르지 않는다. 사전에 규칙을 정하거나(Pesaran–Timmermann 2007식 결합) 윈도 후보 수를 N에 넣는다(Rossi–Inoue 2012).
7. **calibration 점검:** 클래스별 reliability diagram과 ECE를 기록한다. log loss가 좋아도 up 확률 0.6이 실제로 0.45만 맞으면 적중률 판단이 틀어진다.
8. **regime별 분해:** 변동성 3분위, 시간대(장 시작 30분, 장중, 마감 전), 종목별로 클래스별 적중률과 DM t를 나눠 본다. touch target의 up/down 적중이 변동성 예측력에서 오는지 가려낸다(Christoffersen–Diebold 2006).
9. **시점 정렬 회귀 테스트:** feature 타임스탬프가 예측 시각보다 엄격히 앞서는지 자동 검사한다. 라벨 구간과 feature 구간을 교차 확인하는 단위 테스트를 둔다. 앙상블 가중치와 정규화 통계가 과거 데이터만 쓰는지도 확인한다.
10. **(참고, 수익 2순위) 손익분기 표시:** 신호별로 P(up|신호) − P(down|신호)와 C/k를 나란히 표시하면, 적중률이 "거래 가능한 수준"인지 한눈에 보인다. C는 2026년 매도 거래세 0.20%, spread, 수수료를 틱으로 환산한 값이다. 인버스 ETF는 별도 비용표가 필요하다(거래세 면제 여부 미확인).
