# 2023–2026 단기 예측·전략 구성의 새 방법들 (1차 조사에서 빠진 것)

기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다. 1차 조사(`research/reports/단기 매매 퀀트 기법 조사.md`, 노트 05)가 다룬 LOB 딥러닝, cross-sectional ML, triple-barrier·meta-labeling, TSFM·LLM 감성, RL, statistical jump model은 다시 다루지 않는다.

조사 방법: 논문 서지와 초록은 arXiv API(export.arxiv.org)와 Crossref API로 확인했다. 저장소 별 수와 마지막 push는 GitHub API로 2026-10-17에 조회했다. WebSearch는 6회 썼고 모두 동작했다. WebFetch는 PDF 본문을 읽지 못했다(압축 바이너리로 반환). 그래서 논문 본문의 표·수치는 대부분 확인하지 못했다. "(검색 요약)"은 WebSearch 결과 요약에만 근거한 내용이고, "(미검증)"은 원문으로 확인하지 못한 내용이다. 표의 "증거 질"은 아래 기준으로 매긴 이 노트의 판단이다. A = 여러 독립 연구의 표본 밖 재현, B = 탄탄한 단일 연구나 이론 보장, C = 저자 자체 백테스트뿐, D = 반박되었거나 부정 결과.

## 1. 자동 alpha mining (GP, RL formula 생성, LLM agent)과 overfitting 증거

### Takeaway
GP → RL → LLM agent로 탐색기가 바뀌었지만, 근거는 거의 전부 저자 자신의 중국 CSI300/500·S&P 500 일별 cross-section 백테스트다. 독립 재현은 찾지 못했다. 2026년 문헌은 오히려 "고정된 채점 함수에 수렴하는 탐색은 채점이 벌하지 못하는 것에 과적합한다"는 경고 쪽으로 움직였다. 3종목·분 단위·수 주 LOB인 krx-quant에는 맞지 않는다.

### Cited Findings
- **AutoAlpha** (Zhang, Li, Jin, Li 2020): hierarchical evolutionary algorithm에 PCA 기반 Quality Diversity 탐색과 warm start를 더해 formulaic alpha를 캔다. 검증은 중국 시장 백테스트다 — [arXiv 2002.08245](https://arxiv.org/abs/2002.08245)
- **AlphaEvolve** (Cui 등 2021): scalar·vector·matrix feature를 쓰는 새 alpha 종류와 AutoML 기반 탐색. 종목 간 관계 지식을 주입한다 — [arXiv 2103.16196](https://arxiv.org/abs/2103.16196). 이름이 같은 2025년 Google DeepMind의 코드 진화 agent와는 다른 것이다(DeepMind 쪽은 이번에 원문 확인 안 함).
- **AlphaGen** (Yu 등, KDD 2023): alpha를 하나씩 캐지 않고 "조합 모델에 대한 기여도"를 RL(PPO)의 보상으로 써서 synergistic한 alpha 집합을 만든다 — [arXiv 2306.12964](https://arxiv.org/abs/2306.12964), [DOI](https://doi.org/10.1145/3580305.3599831). 저장소는 [ICT-FinD-Lab/alphagen](https://github.com/ICT-FinD-Lab/alphagen)으로 옮겨졌다(별 1,269, 마지막 push 2026-06-04, 라이선스 표시 없음).
- **QuantFactor REINFORCE** (Zhao 등, IEEE TSP 2025): AlphaGen의 PPO가 이 문제에 맞지 않는다고 보고, 분산을 줄인 REINFORCE와 information ratio 보상을 쓴다. 저자 보고로 수익률과의 상관이 3.83% 올랐다 — [arXiv 2409.05144](https://arxiv.org/abs/2409.05144)
- **AlphaForge** (Shi 등 2024): 생성-예측 신경망으로 factor를 만들고, factor 가중치를 최근 성과로 동적으로 바꾼다. "real money investment"에서도 개선됐다고 주장한다(저자 보고) — [arXiv 2406.18394](https://arxiv.org/abs/2406.18394)
- **RiskMiner** (Ren 등 2024): risk-seeking MCTS로 formula를 탐색한다 — [arXiv 2402.07080](https://arxiv.org/abs/2402.07080)
- **Alpha-GPT** (Wang 등 2023): 사람과 LLM이 대화하며 alpha를 만드는 interactive 체계다. 초록의 근거는 "a number of alpha mining experiments"뿐이다 — [arXiv 2308.00016](https://arxiv.org/abs/2308.00016)
- **AlphaAgent** (Tang 등 2025): LLM agent에 세 가지 정규화를 건다. AST 유사도로 기존 alpha와 겹치지 않게 하고, 가설과 formula의 일치를 LLM이 평가하고, AST 구조 제약으로 복잡도를 막는다. 저자 보고로 CSI 500과 S&P 500에서 최근 4년간 alpha decay에 강했다 — [arXiv 2502.16789](https://arxiv.org/abs/2502.16789), 저장소 [RndmVariableQ/AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent)(별 420, push 2026-07-03)
- **RD-Agent(Q)** (Li 등 2025, Microsoft): Research 단계(가설)와 Development 단계(코드 생성 agent Co-STEER)를 bandit scheduler로 돌려 factor와 모델을 함께 최적화한다. 저자 보고로 고전 factor library보다 factor를 70% 덜 쓰고 연 수익률이 최대 2배였다 — [arXiv 2505.15155](https://arxiv.org/abs/2505.15155), [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent)(별 14,958, MIT, push 2026-10-16), 기반 플랫폼 [microsoft/qlib](https://github.com/microsoft/qlib)(별 49,432, MIT)
- **LLM + MCTS** (Shi, Duan, Li 2025): backtest 피드백으로 MCTS를 이끌고 자주 나온 subtree를 피해 다양성을 지킨다 — [arXiv 2505.11122](https://arxiv.org/abs/2505.11122). 저자들이 누설을 직접 점검했다. GPT-4.1, Gemini 2.0 Flash-Lite, DeepSeek-V3에게 CSI 300용 "고성능 formula"를 쓰라고 하자 무작위 formula와 성과가 유의하게 다르지 않았다(검색 요약)
- **EFS** (Chen 등 2025): 시험 구간을 backbone 학습 cutoff 이후로 두고, LLM 검색을 끄고, 날짜를 prompt에서 가렸다(저자 자체 통제, 검색 요약) — [arXiv 2507.17211](https://arxiv.org/abs/2507.17211)
- **AlphaBench** (Luo 등, ICLR 2026): LLM formulaic alpha mining의 첫 체계적 benchmark다. 결론은 LLM이 "strong potential"이 있지만 robustness, 탐색 효율, 실용성에 "persistent challenges"가 남았다는 것이다. 페이지 초록은 표본 밖 일반화를 따로 다루지 않는다 — [ICLR 2026](https://iclr.cc/virtual/2026/poster/10008434), [OpenReview](https://openreview.net/forum?id=d97Q8r7ZKZ)
- **2026년의 경고**: "A search that converges against a fixed scorer overfits whatever the scorer cannot penalize"(Li, Liu, Liu 2026, Sealed Joint Search) — [arXiv 2606.29194](https://arxiv.org/abs/2606.29194). AlphaMemo도 과거 성공을 단순 재사용하면 overfitting 위험이 있다고 썼다 — [arXiv 2606.20625](https://arxiv.org/abs/2606.20625)
- **"The Alpha Illusion"** (Ye 등 2026, position paper): LLM *trading agent*(FinCon, FinMem, TradingAgents 등)의 보고 alpha는 배포 근거가 아니라고 주장한다. 지금 공개된 증거로는 temporal contamination, 모델에 넣지 않은 마찰, 짧은 구간의 Sharpe 불확실성을 가려낼 수 없다고 보고, 보고 규약 P1–P6을 낸다. factor mining이 아닌 agent 대상이다 — [arXiv 2605.16895](https://arxiv.org/abs/2605.16895)
- **데이터 마이닝 일반의 기준선**: 회계 비율 29,000개에서 t > 2인 것을 고르면 동료심사 논문과 비슷한 cross-sectional 예측력이 나오고, 둘 다 원 표본 뒤에 예측력의 약 50%가 남았다(Chen, Lopez-Lira, Zimmermann) — [arXiv 2212.10317](https://arxiv.org/abs/2212.10317)
- 과적합 측정 도구: Probability of Backtest Overfitting(Bailey, Borwein, López de Prado, Zhu) — [DOI](https://doi.org/10.21314/JCF.2016.322); Deflated Sharpe Ratio(Bailey & López de Prado) — [SSRN](https://doi.org/10.2139/ssrn.2460551); 여러 신호를 골라 합친 backtest의 편향(Novy-Marx) — [NBER w21329](https://doi.org/10.3386/w21329)(초록 미확인)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| GP (AutoAlpha 류) | C | 중간 (수만 번 backtest, CPU) | 낮음 |
| RL formula (AlphaGen, QFR) | C | 중~높음 (GPU 권장, 수많은 평가) | 낮음 |
| LLM agent (Alpha-GPT, AlphaAgent, RD-Agent) | C, 누설 점검은 저자 자체 | 높음 (LLM API 또는 GPU) | 매우 낮음 |
- 이 방법들은 수백 종목의 IC(cross-sectional 순위 상관)를 목적함수로 쓴다. 3종목의 시계열 방향 예측에서는 IC가 정의되지 않고, 표본이 작아 탐색 공간 대비 과적합 위험이 훨씬 크다.
- Chen 등의 결과를 거꾸로 읽으면 이렇다. 데이터 마이닝 신호의 표본 밖 감쇠는 동료심사 신호와 비슷하다(약 절반 남음). 그러니 자동 mining을 하더라도 시도 횟수를 기록하고 문턱을 올리는 것(1차 보고서의 채점 보강, #262)이 핵심이고, 탐색기의 종류는 부차적이다.
- 가져올 만한 아이디어는 탐색기가 아니라 정규화 쪽이다. AlphaAgent의 "기존 신호와의 유사도 제한"과 "복잡도 제한"은 krx-quant에 새 알고리즘이나 피처를 넣을 때의 검사 규칙으로 쓸 수 있다.

### Gaps
- AlphaGen·AlphaAgent·RD-Agent 결과를 제3자가 다른 기간이나 시장에서 재현한 연구는 찾지 못했다.
- 각 논문이 거래비용을 넣었는지, 넣었다면 얼마로 넣었는지는 PDF를 읽지 못해 확인하지 못했다.
- 분 단위·소수 종목에서 GP나 RL alpha mining을 시험한 연구는 찾지 못했다.

## 2. Path signature / rough path feature

### Takeaway
Signature는 경로를 iterated integral 수열로 바꾸는 범용 피처 변환이다. 이론은 탄탄하다. 2026년에는 signature 회귀의 근사·통계 이론이 처음 나왔다. 그러나 주식 장중 방향 분류를 표본 밖에서 검증한 연구는 찾지 못했다. 금융 실증은 실행, 포트폴리오, 변동성 예측, pairs trading 쪽이다. 계산이 싸서 시험 비용은 낮다.

### Cited Findings
- 원조: Lyons, "Differential equations driven by rough signals", *Rev. Mat. Iberoam.* 14:215–310 (1998) — [DOI](https://doi.org/10.4171/rmi/240). 입문: Chevyrev & Kormilitzin 2016 primer. signature는 경로를 "a sequence of numbers"로 요약하는 non-parametric 피처 변환이다 — [arXiv 1603.03788](https://arxiv.org/abs/1603.03788)
- 첫 금융 적용(2013): 적은 수의 signature 계수로 데이터 스트림을 분류했다. WTI 원유 선물의 30분 구간에서 비정상 시장 행동을 찾고, FTSE 100 선물에서 두 실행 알고리즘의 parent order를 구별했다 — [Gyurkó, Lyons, Kontkowski, Field, arXiv 1307.7244](https://arxiv.org/abs/1307.7244)
- 최적 실행: 거래 속도를 signature의 선형 함수로 근사해 풀었다 — [Kalsi, Lyons, Perez Arribas 2019, arXiv 1905.00728](https://arxiv.org/abs/1905.00728)
- **Signature Trading** (Futter, Horvath, Wiese 2023): 전략을 "signal과 자산의 결합 경로" signature의 선형 함수로 두고, dynamic mean-variance의 명시해를 얻는다. 해에는 drawdown 제어가 들어간다. momentum과 pairs trading 예를 합성 데이터와 시장 데이터로 보였다(저자 보고, 비용 처리는 미검증) — [arXiv 2308.15135](https://arxiv.org/abs/2308.15135)
- **이론** (Horvath 등 2026): signature 회귀의 첫 근사율과 통계 이론이다. Signature-OLS, -LASSO, -Logistic의 일관성을 보였다. 응용에서는 장중 가격 경로로 FX realized volatility를 예측해, 손으로 만든 피처보다 나았다 — [arXiv 2607.17865](https://arxiv.org/abs/2607.17865)
- LOB에 적용(IEEE Access 2023, 검색 요약): mean-mid 가격 예측에서 Sig-DNN은 raw LOB를 쓴 DNN보다 나았지만, Sig-RF는 raw LOB를 쓴 RF보다 못했다 — [UJ 저장소](https://pure.uj.ac.za/en/publications/a-signature-transform-of-limit-order-book-data-for-stock-price-pr/)
- 분 단위 선물 pairs trading(Guo 등 2025): signature를 두 지표로 분해해 이중 필터로 썼고, 전통 pairs보다 수익·MDD·Sharpe가 나았다(저자 보고) — [arXiv 2505.05332](https://arxiv.org/abs/2505.05332)
- 라이브러리(2026-10-17 GitHub): [iisignature](https://github.com/bottler/iisignature)(별 125, push 2026-06, MIT), [signatory](https://github.com/patrick-kidger/signatory)(별 313, 마지막 push 2024-01), [RoughPy](https://github.com/datasig-ac-uk/RoughPy)(별 58, push 2026-07), [pySigLib](https://github.com/daniil-shmelev/pySigLib)(별 27, push 2026-10-17)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| signature 피처 + logistic/LightGBM | B(이론), 방향 예측 실증은 없음 | 매우 낮음 | 중간 (싼 실험) |
| Sig-Trading (포트폴리오 최적화) | C | 낮음 | 낮음 (signals-only 엔진은 포지션을 정하지 않음) |
- 차원은 채널 d, 깊이 m일 때 d + d² + … + d^m이다. 예를 들어 (시간, log mid, 누적 OFI) 3채널을 깊이 3까지 쓰면 39개, 깊이 4면 120개다. 2 GB 서버에서도 1분봉마다 계산할 수 있다.
- 깊이 2의 반대칭 항(Lévy area)은 두 채널 중 어느 쪽이 먼저 움직였는지를 잰다. 그래서 (선물, 현물) 경로의 Lévy area는 Granger·Kalman이 쓰는 lead-lag 정보를 피처 하나로 압축한다(5절과 연결).
- Horvath 등(2026)의 Signature-Logistic은 Shannon(OFI logistic)에 바로 겹쳐 시험할 수 있다. "손으로 만든 피처보다 낫다"는 결과는 변동성 예측에서 나온 것이라, 방향에서도 그럴지는 미지수다.

### Gaps
- 주식 장중 up/flat/down 분류에서 signature 피처의 표본 밖 효과를 잰 연구는 찾지 못했다.
- Bennett 등(2022, 5절)이 Lévy area를 lead-lag 척도 중 하나로 비교했다는 것은 기억에 의한 것이다. 초록은 "a number of choices for the pairwise lead-lag metric"까지만 확인됐다(PDF 미확인).

## 3. Conformal prediction과 확률 calibration (up/flat/down)

### Takeaway
Conformal은 hit rate를 올리지 않는다. 대신 "이 정도 확신이면 몇 %가 맞는다"를 분포 가정 없이 보장하는 abstention·구간 도구다. 시계열의 교환가능성 문제는 ACI 계열(2021–2023)이 온라인 보정으로 풀었다. 그러나 금융 구간 예측에서 2025년 비교 연구는 변동성 모델이 interval score로 더 자주 이겼다. 다범주 확률 보정은 temperature scaling(모수 1개)이 가장 싸고, isotonic은 보정 표본이 약 1,000개보다 적으면 과적합한다.

### Cited Findings
- 원조: Vovk, Gammerman, Shafer, *Algorithmic Learning in a Random World* (2판 2022) — [DOI](https://doi.org/10.1007/978-3-031-06649-8)
- **ACI** (Gibbs & Candès 2021): 분포가 시간에 따라 바뀌어도 하나의 모수(유효 α)를 온라인 경사로 갱신해, 긴 구간의 coverage 빈도를 데이터 생성 과정과 무관하게 보장한다 — [arXiv 2106.00170](https://arxiv.org/abs/2106.00170). 실험 하나가 GARCH(1,1) 기반 시장 변동성 예측이다(검색 요약)
- **DtACI** (Gibbs & Candès 2022): ACI의 step size도 온라인으로 고른다. 주식시장 변동성과 COVID-19 데이터로 시험했다 — [arXiv 2208.08401](https://arxiv.org/abs/2208.08401)
- **AgACI** (Zaffran 등 2022): 여러 학습률의 ACI를 online expert aggregation으로 합친 parameter-free 방법이다. 전력 가격으로 시험했다 — [arXiv 2202.07282](https://arxiv.org/abs/2202.07282)
- **Conformal PID** (Angelopoulos, Candès, Tibshirani, NeurIPS 2023): 제어 이론으로 score를 예측해 계절성·추세·shift에 적응한다. 시장 수익률 실험을 포함한다 — [arXiv 2307.16895](https://arxiv.org/abs/2307.16895), [코드](https://github.com/aangelopoulos/conformal-time-series)(별 147, 마지막 push 2023-11)
- 비교환 데이터: weighted quantile로 drift에 강하게 만든 NexCP — [Barber 등, arXiv 2202.13415](https://arxiv.org/abs/2202.13415); 잔차의 자기상관을 이용한 SPCI — [Xu & Xie, arXiv 2212.03463](https://arxiv.org/abs/2212.03463)
- 분류용 conformal set: APS(adaptive score) — [Romano, Sesia, Candès, NeurIPS 2020, arXiv 2006.02544](https://arxiv.org/abs/2006.02544); RAPS(드문 class의 작은 score를 정규화해 set을 안정화, Platt 단독보다 set이 5~10배 작음) — [Angelopoulos 등, arXiv 2009.14193](https://arxiv.org/abs/2009.14193). 구현: [MAPIE](https://github.com/scikit-learn-contrib/MAPIE)(별 1,603, BSD-3, push 2026-10-16)
- **금융의 정직한 비교** (Aich 등 2025, TCP v7): 13개 금융 시계열, 공통 1,448일, 목표 coverage 95%. S&P 500의 coverage는 95.23%로 목표에 맞았다. 하지만 TCP와 변동성 기반 benchmark 4개를 interval score로 짝지어 비교한 52건 가운데 **42건은 변동성 모델이 이겼고** 10건은 유의한 차이가 없었다(다중검정 보정 없음). 전체 coverage가 맞아도 하방 꼬리 보정은 보장되지 않았다. split-conformal 보장은 교환가능성을 요구하므로 의존적인 수익률에 자동으로 적용되지 않는다 — [arXiv 2507.05470](https://arxiv.org/abs/2507.05470)
- VaR에서 DtACI는 AgACI보다 중앙 구간 폭이 최대 33% 좁으면서 명목 coverage를 지켰다(검색 요약, 원문 미확인) — [PDF](https://khuong.uk/Papers/Conformal_prediction_VaR.pdf). 일별 수익률에서 ICP는 coverage는 견고하지만 구간이 넓고, 고전 변동성 모델이 여전히 경쟁력 있었다(석사 논문, 검색 요약) — [Tampere](https://trepo.tuni.fi/handle/10024/232978)
- **Calibration**: temperature scaling(Platt scaling의 모수 1개 버전)은 대부분의 데이터에서 "surprisingly effective"였다 — [Guo 등 2017, arXiv 1706.04599](https://arxiv.org/abs/1706.04599). Dirichlet calibration은 어떤 분류기에도 쓸 수 있는 본래 다범주 방법이고, log 확률 위의 선형층 + softmax와 같다. log-loss, Brier, classwise-ECE가 개선됐다 — [Kull 등, NeurIPS 2019, arXiv 1910.12656](https://arxiv.org/abs/1910.12656)
- scikit-learn 문서: isotonic은 "more prone to overfitting, especially on small datasets"이고, 표본이 약 1,000개를 넘으면 sigmoid만큼 또는 더 낫다. 다범주는 one-vs-rest로 보정한 뒤 정규화한다. `method="temperature"`가 있으며 모수 하나로 다범주를 보정하고 정확도는 바꾸지 않는다 — [scikit-learn calibration](https://scikit-learn.org/stable/modules/calibration.html)
- 고전 비교: Niculescu-Mizil & Caruana(ICML 2005), "Predicting good probabilities with supervised learning" — [DOI](https://doi.org/10.1145/1102351.1102430)(본문은 PDF를 읽지 못해 이번에 확인 못 함). Venn-Abers는 isotonic 기반으로 iid에서 calibration을 보장하는 이진 예측기다 — [Vovk & Petej, arXiv 1211.0025](https://arxiv.org/abs/1211.0025)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| temperature scaling (구성원별) | B (일반 ML), 금융 특화 증거 없음 | 거의 0 | **높음** |
| Dirichlet calibration | B | 낮음 (3×3 + 3 모수) | 중간 |
| isotonic (class별) | B, 작은 표본에서 과적합 | 낮음 | 보정 창이 클 때만 |
| APS/RAPS + ACI 분류 set | B (이론), 금융 방향 실증 없음 | 낮음 | 중간 (#266과 겹침) |
| ACI/DtACI로 Mandelbrot 구간 보정 | B/C, 변동성 모델이 자주 이김 | 낮음 | 중간 (대체가 아닌 덧씌움) |
- Condorcet의 log-loss 가중은 구성원 확률의 보정 상태에 크게 흔들린다. 과신하는 구성원 하나가 큰 log-loss로 가중치를 잃거나, 반대로 둔한 구성원이 과대평가될 수 있다. 가중 전에 구성원별 temperature를 최근 며칠로 맞추는 것이 가장 싼 개선이다.
- 보정 창의 크기를 따져 보면 이렇다. 5거래일 × 390분 ≈ 1,950개 1분봉에서, flat이 다수면 up·down 표본은 각각 1,000개를 밑돌 수 있다. 그러면 class별 isotonic보다 temperature(모수 1)나 Dirichlet가 안전하다.
- Conformal 분류 set은 "set이 {up} 또는 {down} 하나일 때만 신호"라는 규칙을 준다. 이는 coverage를 내주고 precision을 올리는 meta-labeling(#266)과 같은 역할이다. 그래서 둘을 같은 리플레이에서 비교해야 한다. 보장은 marginal coverage일 뿐이고, 신호를 낸 표본만의 hit rate를 보장하지는 않는다.
- TCP 결과를 보면, range 종류는 HAR(Mandelbrot)을 conformal로 *대체*하지 말고 HAR 구간에 ACI 보정을 *덧씌워* coverage만 맞추는 편이 근거와 맞는다.

### Gaps
- 분 단위 주식 방향 3범주에 conformal set이나 ACI를 쓴 연구는 찾지 못했다.
- LightGBM multiclass(log-loss 학습)가 금융 분 단위에서 얼마나 보정되어 있는지 잰 연구는 찾지 못했다. scikit-learn 문서도 gradient boosting은 다루지 않는다.

## 4. Online learning과 adaptive ensemble

### Takeaway
expert 가중을 손실에 따라 지수적으로 바꾸는 방법(EWA, fixed-share, BOA)과 forgetting factor를 둔 Bayesian model averaging(DMA)은 이론과 거시·월간 금융 예측에서 근거가 있다. 그러나 장중 금융에서 정적 가중이나 단순 평균을 이겼다는 독립 근거는 찾지 못했다. "추정한 결합 가중치가 단순 평균을 자주 못 이긴다"는 forecast combination puzzle이 기본 반론이다.

### Cited Findings
- **Fixed share** (Herbster & Warmuth 1998): 최고의 expert가 시간에 따라 바뀌는 상황을 추적하는 지수 가중 알고리즘 — [Machine Learning 32:151–178](https://doi.org/10.1023/a:1007424614876)
- **BOA** (Wintenberger 2017): 지수 가중에 2차 항(quadratic variation 대리)을 더해 안정화하고, 2차 regret bound를 얻었다 — [arXiv 1404.1356](https://arxiv.org/abs/1404.1356). R 패키지 [opera](https://github.com/Dralliag/opera)(별 56, push 2026-03)가 이런 aggregation 규칙들을 구현한다(포함된 규칙 목록은 미검증)
- **DMA** (Raftery, Kárný, Ettler 2010): forgetting factor로 모형 확률과 계수를 함께 시간에 따라 바꾸는 dynamic model averaging — [Technometrics 52:52–66](https://doi.org/10.1198/TECH.2009.08104)
- 거시: DMA가 미국 분기 인플레이션 예측에서 단순 benchmark와 time-varying coefficient 모형을 크게 이겼다 — [Koop & Korobilis 2012, IER 53:867–886](https://doi.org/10.1111/j.1468-2354.2012.00704.x)
- 주식: 월간 S&P 500에서 time-varying coefficient 예측 회귀(베이지안 모형 평균)가 표본 밖 예측력을 보였다. 역사적 평균 대신 이 방법을 쓴 투자자의 효용 이득은 기간에 따라 연 약 1.8~5.8%였고, 예측력은 경기순환과 연결됐다 — [Dangl & Halling 2012, JFE 106:157–181](https://doi.org/10.1016/j.jfineco.2012.04.003), [RePEc 요약](https://ideas.repec.org/a/eee/jfinec/v106y2012i1p157-181.html)
- 암호화폐(일별): DMA로 단변량 모형을 결합하면 점 예측이 유의하게 좋아졌고, 방향 예측력도 상당했다 — [Catania, Grassi, Ravazzolo 2019, IJF 35:485–501](https://doi.org/10.1016/j.ijforecast.2018.09.005), [RePEc](https://ideas.repec.org/a/eee/intfor/v35y2019i2p485-501.html)
- Forecast combination puzzle: [Smith & Wallis 2009, OBES 71:331–355](https://doi.org/10.1111/j.1468-0084.2008.00541.x); [Claeskens 등 2016, IJF 32:754–762](https://doi.org/10.1016/j.ijforecast.2015.12.005). 50년 문헌 리뷰는 단순 결합부터 시변 가중, 상관 고려, cross-learning까지 정리하고 각 방법의 한계를 논한다 — [Wang, Hyndman, Li, Kang, arXiv 2205.04216](https://arxiv.org/abs/2205.04216)
- 장중 실무 사례(1차 보고서 인용): Optiver Trading at the Close 1위는 12일마다 다시 학습하는 online learning으로 MAE를 5.4523에서 5.4165로 줄였다 — [Kaggle](https://www.kaggle.com/competitions/optiver-trading-at-the-close/discussion/487446)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| EWA / fixed-share (log loss) | B (이론), 장중 금융 실증 없음 | 거의 0 (구성원 수 K에 비례) | **높음** (Condorcet 대체 후보) |
| BOA | B | 거의 0 | 중간 |
| DMA (forgetting) | B (거시·월간), 장중 미확인 | 낮음 | 중간 |
- log loss를 쓰는 EWA는 forgetting이 있는 Bayesian model averaging과 같은 꼴이다. 그래서 Condorcet의 "밤마다 log-loss로 다시 매기는 가중"을 그대로 두고 갱신만 장중으로 옮길 수 있다. 4분 정답이 확정될 때마다 갱신하면 된다.
- puzzle 때문에 비교 기준선에는 반드시 동일 가중을 넣어야 한다. 가중을 동일 가중 쪽으로 shrink하는 것(예: fixed-share의 share 비율, 또는 가중치 하한)도 같이 시험해야 한다.
- 방향 구성원이 9개(Newton~Breiman)이고 정답이 4분 늦게 확정되면, 하루 표본은 종목당 약 97개(390/4) 비중첩 창이다. 장중 가중 변화의 대부분은 잡음일 수 있다. 가중 경로의 변동을 기록해서 국면 전환과 맞물리는지 봐야 한다.

### Gaps
- 분 단위 주식 방향 예측에서 EWA·fixed-share·DMA를 정적 가중·단순 평균과 비교한 연구는 찾지 못했다.

## 5. Graph·lead-lag 방법 (Granger 너머)

### Takeaway
고빈도 lead-lag는 확실히 존재한다(선물→현물, 유동성 큰 종목→작은 종목). 다음 midquote 방향을 60% 맞히지만 시장가 매매로는 스프레드 때문에 이익이 없다(Huth & Abergel). 비동기 tick에서는 Hayashi–Yoshida 계열 추정량이 표준이다. GNN은 수백 종목 관계용이라 3종목에는 의미가 없다.

### Cited Findings
- **Hayashi–Yoshida** (2005): 비동기로 관측된 확산 과정의 공분산 추정량 — [Bernoulli 11:359–379](https://doi.org/10.3150/bj/1116340299)
- **Lead-lag 모수 추정** (Hoffmann, Rosenbaum, Yoshida 2013): 시간 이동 ϑ를 lead-lag 모수로 두고, 수정한 Hayashi–Yoshida covariation의 contrast 최적화로 일관 추정량과 수렴 속도를 얻었다 — [arXiv 1303.4871](https://arxiv.org/abs/1303.4871), Bernoulli 19(2):426–461
- **실증** (Huth & Abergel): tick 데이터에서 cross-correlation이 강하게 비대칭이고, 특히 선물/현물 쌍에서 그렇다. 유동성 큰 자산이 앞선다. lead-lag는 장중 계절성이 있어 거시지표 발표와 미국 개장에서 바뀐다. 앞서는 자산의 과거만으로 뒤따르는 자산의 다음 midquote 변화를 **60%** 맞혔다. 그러나 "a naive strategy based on market orders cannot make any profit of this effect because of the bid/ask spread" — [arXiv 1111.7103](https://arxiv.org/abs/1111.7103)
- **네트워크 군집** (Bennett, Cucuringu, Reinert 2022): 쌍별 lead-lag를 방향 그래프로 보고, 불균형이 큰 lead-lag 군집 쌍을 찾는다. 미국 주식 **일별** 데이터에서 유의한 군집을 찾았고 예측 신호를 만들었다 — [arXiv 2201.08283](https://arxiv.org/abs/2201.08283)
- 후속: 슬라이딩 창 부분 시계열을 군집화해 lead-lag를 robust하게 집계 — [Zhang, Cucuringu, Shestopaloff, Zohren 2023, arXiv 2305.06704](https://arxiv.org/abs/2305.06704); DTW판 — [arXiv 2309.08800](https://arxiv.org/abs/2309.08800); 쌍마다 lag가 변하는 것을 cross-attention으로 배우는 DeltaLag(저자 보고로 고정 lag와 통계 기반 lead-lag 그래프보다 나음) — [arXiv 2511.00390](https://arxiv.org/abs/2511.00390)
- **GNN**: 업종·공급망 관계를 그래프로 넣는 Relational Stock Ranking — [Feng 등, TOIS 2019, arXiv 1809.09441](https://arxiv.org/abs/1809.09441)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| Hayashi–Yoshida lead-lag 함수 (tick) | B (이론 + 실증 반복) | 낮음 (쌍 × lag 격자) | **높음** (피처·진단) |
| Lévy area / signature lead-lag | C | 매우 낮음 | 중간 |
| 군집·DTW·DeltaLag | C | 중~높음 | 낮음 (종목이 3개) |
| GNN | C | 높음 | 없음 |
- Granger(분봉 VAR)는 동기화된 봉을 쓰므로 초 단위 lead-lag를 봉 경계에서 흐린다. KOSPI200 선물 → 세 종목, 삼성전자 ↔ SK하이닉스를 Hoffmann–Rosenbaum–Yoshida 방식으로 tick에서 재면 최적 lag ϑ와 그 장중 변화를 얻는다. 이것을 Granger·Kalman의 lag 선택과 피처로 쓸 수 있다.
- Huth & Abergel의 60%는 다음 1틱 변화에 대한 것이다. 4분 horizon까지 남는 정보는 그보다 훨씬 작을 것이다. 그래서 lead-lag는 단독 신호가 아니라 피처로, 그리고 "미국 개장이나 지표 발표 때 구조가 바뀐다"는 진단으로 쓰는 편이 맞다.

### Gaps
- KRX(KOSPI200 선물과 대형주)의 tick lead-lag를 HY 계열로 잰 공개 연구는 이번에 찾지 못했다.
- Bennett 등이 비교한 lead-lag 척도 목록과 성과 수치는 PDF를 읽지 못해 확인하지 못했다.

## 6. 평균회귀 spread의 최적 진입 규칙과 Kalman 동적 hedge ratio

### Takeaway
OU 과정의 최적 진입·청산(Bertram 2010, Leung & Li 2015)은 해석해가 있고, 비용과 stop-loss가 문턱을 어떻게 옮기는지 알려 준다. 그러나 단순 z-score 규칙보다 표본 밖에서 낫다는 독립 실증은 찾지 못했다. Kalman hedge ratio도 rolling OLS와 같은 조건, 비용 포함, walk-forward로 정면 비교한 학술 연구를 찾지 못했다.

### Cited Findings
- **Bertram** (2010): 가격이 OU를 따를 때 통계적 차익거래의 해석식을 first-passage time 문제로 유도했다. 거래 길이와 수익의 평균·분산을 구하고, 기대수익 최대화와 Sharpe 최대화를 비교했으며, 기대수익 최대화는 정확한 해석해를 얻었다 — [Physica A 389:2234–2243](https://doi.org/10.1016/j.physa.2010.01.045), [RePEc 요약](https://ideas.repec.org/a/eee/phsmap/v389y2010i11p2234-2243.html)
- **Leung & Li** (2015): 거래비용을 넣은 최적 double stopping 문제다. 진입 영역은 stop-loss 수준보다 엄격히 위에 있는 유계 구간이고, stop-loss를 높이면 최적 take-profit은 낮아진다 — [arXiv 1411.5062](https://arxiv.org/abs/1411.5062), IJTAF 18(3)
- Zeng & Lee(2014), "Pairs trading: optimal thresholds and profitability" — [Quant. Finance 14:1881–1893](https://doi.org/10.1080/14697688.2014.917806)(초록을 얻지 못함)
- Kalman 원조: Elliott, van der Hoek, Malcolm, "Pairs trading" — [Quant. Finance 5:271–276 (2005)](https://doi.org/10.1080/14697680500149370)(spread를 상태공간 모형으로 둔다는 내용은 기억 기반, 미검증)
- 리뷰: Krauss, "Statistical arbitrage pairs trading strategies: review and outlook" — [J. Econ. Surveys 31:513–545](https://doi.org/10.1111/joes.12153)(본문 미확인)
- 정면 비교 부재(검색 요약): 비용을 꼼꼼히 넣은 학술 연구로는 중국 상품선물 고빈도의 Kalman+MCMC 혼합 모형 석사 논문이 있다. 저자 모형이 유리했지만 Kalman 대 OLS를 분리하지 않았다 — [Waterloo](https://uwspace.uwaterloo.ca/handle/10012/12793). 이란 자동차주 26종목 연구는 Kalman이 공적분보다 나았다(Sharpe 3.06)고 했지만 표본이 작다 — [Financial Research Journal](https://jfr.ut.ac.ir/article_92806_en.html). 실무 쪽은 모수 민감도를 경고한다 — [Robot Wealth](https://robotwealth.com/kalman-filter-pairs-trading-with-zorro-and-r/), [PairTrade Finder](https://pairtradefinder.com/?p=7761)
- 구현: [arbitragelab](https://github.com/hudson-and-thames/arbitragelab)(별 701, BSD-3, 마지막 push 2024-05; 구현 목록은 미검증)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| OU 최적 진입 (Bertram, Leung–Li) | B (이론), 실증 C | 매우 낮음 | 조건부 (κ > 0일 때만) |
| Kalman 동적 hedge ratio | C | 낮음 | 낮음 (pairs 엔진이 아님) |
- 1차 보고서에 따르면 Gauss는 2026-09~10 학습 창에서 되돌림 속도 κ = 0을 배웠다. κ ≈ 0이면 OU 최적 문턱 이론은 줄 것이 없다. 먼저 κ > 0이 유의한 조건(개장 10분, 극단 이탈 등)을 찾아야 하고, 그 조건 안에서만 Leung–Li식 문턱을 고려할 만하다.
- 최적 문턱 이론에서 가장 쓸모 있는 교훈은 "비용이 진입 문턱을 바깥으로 민다"이다. 이는 1차 보고서의 C/k 손익분기 식과 같은 방향이다. krx-quant의 비용 bp 상수를 Gauss 신호 문턱에 직접 넣는 것으로 충분히 반영된다.
- Kalman 엔진은 이미 microprice와 선물을 쓴다. 삼성전자–SK하이닉스 spread에 동적 hedge ratio를 붙이는 것은 상대강도 종류(1차 보고서 8번)를 만들 때 다시 볼 일이다.

### Gaps
- 같은 쌍 선택, 같은 창, 비용 sweep, walk-forward로 Kalman과 rolling OLS, OU 최적 문턱과 고정 z-score를 비교한 학술 연구는 찾지 못했다.

## 7. Causal·robust 접근 (IRM, causal discovery, adversarial validation)

### Takeaway
IRM은 이론적으로 쉽게 실패하고, 세심하게 구현한 ERM이 domain generalization benchmark에서 최고 수준이었다. 금융에 IRM이나 causal representation을 쓴 연구는 저자 자체 결과뿐이다. 바로 쓸 만한 것은 싼 진단 도구, 즉 adversarial validation(학습 창과 새 데이터를 구별하는 분류기)이다.

### Cited Findings
- **IRM** (Arjovsky, Bottou, Gulrajani, Lopez-Paz 2019): 여러 학습 분포에서 최적 분류기가 같아지는 표현을 배워 OOD 일반화를 노린다 — [arXiv 1907.02893](https://arxiv.org/abs/1907.02893)
- **반론** (Rosenfeld, Ravikumar, Risteski 2020): 선형에서도 IRM은 최적 불변 예측기를 찾는 데 "more often, fails"이고, 비선형에서는 시험 데이터가 학습 분포와 충분히 비슷하지 않으면 "fail catastrophically"할 수 있다 — [arXiv 2010.05761](https://arxiv.org/abs/2010.05761)
- **DomainBed** (Gulrajani & Lopez-Paz 2020): 7개 데이터셋, 9개 알고리즘에서 세심하게 구현한 ERM이 모든 데이터셋에서 최고 수준이었다 — [arXiv 2007.01434](https://arxiv.org/abs/2007.01434), [코드](https://github.com/facebookresearch/DomainBed)(보관 처리됨)
- 금융 적용(Xu, Cheng, Lee 2025): multi-factor 모형으로 인과 관계를 넣은 오차 상한을 만들고, causal discovery로 불변 피처 표현을 찾았다. 결과는 저자 수치 실험이다 — [arXiv 2503.20987](https://arxiv.org/abs/2503.20987). López de Prado, *Causal Factor Investing* (Cambridge Elements 2023) — [DOI](https://doi.org/10.1017/9781009397315)(본문 미확인)
- **PCMCI** (Runge 등, Science Advances 2019): 조건부 독립 검정과 causal discovery를 결합해 큰 비선형 시계열에서 인과 네트워크를 찾는다 — [arXiv 1702.07007](https://arxiv.org/abs/1702.07007), [tigramite](https://github.com/jakobrunge/tigramite)(별 1,731, **GPL-3.0**)
- **Adversarial validation** (Pan 등 2020, Uber): 새 데이터의 concept drift를 성능이 떨어지기 *전에*, 추론 전에 찾는다. AutoML3 데이터와 Uber 내부 시스템에서 시험했다 — [arXiv 2004.03045](https://arxiv.org/abs/2004.03045)

### Inferences
| 방법 | 증거 질 | 계산 비용 | krx-quant 적합도 |
|---|---|---|---|
| IRM / causal representation | D (일반 ML), 금융 C | 중간 | 낮음 |
| PCMCI | B (기후 등), 금융 분 단위 미확인 | 중간 | 낮음~중간 (진단용) |
| Adversarial validation | B (실무 반복 사용), 금융 실증 적음 | 낮음 (LightGBM 1회) | **높음** (drift 경보) |
- 밤마다 "학습 창 vs 최근 1~2일"을 구별하는 LightGBM을 하나 더 돌리면 된다. AUC가 0.5에서 멀어지면 drift이고, 중요도가 큰 피처가 drift 원인이다. 이것은 #262 채점 보강의 진단 지표로 넣을 수 있고, 같은 근거로 피처를 빼는 실험을 할 수 있다.
- tigramite는 GPL-3.0이다. krx-quant는 공개 저장소이므로 의존성으로 넣으려면 라이선스를 먼저 따져야 한다.

### Gaps
- 분 단위 주식 예측에서 IRM·causal discovery가 ERM(LightGBM)을 표본 밖에서 이겼다는 독립 연구는 찾지 못했다.

## 8. 2025–2026 intraday 예측의 정직한 결과(부정 결과 포함)

### Takeaway
2025–2026년의 정직한 결과는 세 갈래다. 평가 지표 자체가 trivial 예측기를 유리하게 만든다는 지적(CZAR), conformal이 단순 변동성 모델을 못 이긴다는 비교(TCP), LLM agent가 장중에 오히려 음의 timing을 보인다는 측정(RetailAgent)이다. 긍정 결과로는 고빈도 cross-section 수익률로 장중 시장 수익률을 예측한 연구(Aleti 등)와, 파라미터 7천 개 transformer가 큰 모델과 비슷한 LOB 예측을 낸 연구(MBOFormer)가 있다.

### Cited Findings
- **CZAR loss** (Pfeffer 등 2026, ADI 3:33–54): 로그수익률의 조건부 평균이 0에 가까워서, MSE·MAE 같은 대칭 손실은 상수 0 예측을 거의 최적으로 만든다. 학습에서는 예측이 0으로 줄고, 평가에서는 trivial 예측기가 순위를 이끌 수 있다. 모든 대칭 단조 손실에는 0 예측을 이기는 "breakeven directional accuracy"가 있고, 예측 잡음이 수익률 표준편차에 가까워지면 이 값이 급격히 올라 도달할 수 없게 된다. 장중 BTC LightGBM 실험에서 CZAR로 학습하면 큰 움직임의 방향 정확도가 올랐다 — [arXiv 2609.36061](https://arxiv.org/abs/2609.36061)
- **TCP** (Aich 등 2025): 3절 참조. 52건 중 42건에서 변동성 모델이 conformal 구간을 이겼다 — [arXiv 2507.05470](https://arxiv.org/abs/2507.05470)
- **RetailAgent** (Zhang 등 2026): LLM이 익명화한 장중 가격 이력을 보고 long/flat을 반복해 고르게 했다. 노출을 맞춘 측정에서 modality, horizon, 상태, 모델 계열 전반에 걸쳐 지속적인 **음의 timing**이 나왔다. 행동 순서를 섞으면 효과가 크게 줄었다 — [arXiv 2608.28399](https://arxiv.org/abs/2608.28399)
- **The Alpha Illusion** (2026): 1절 참조 — [arXiv 2605.16895](https://arxiv.org/abs/2605.16895)
- **긍정, Aleti, Bollerslev, Siggaard** (Management Science 2025): lag된 고빈도 cross-section 수익률("factor zoo")에 ML을 써서 장중 시장 수익률을 예측했다. 단순 ETF 전략에서 거래비용 뒤에도 표본 밖 Sharpe와 alpha가 컸고, 성과는 경제 불확실성이 높은 기간에 몰렸다(검색 요약) — [DOI](https://doi.org/10.1287/mnsc.2023.01657), [SSRN](https://doi.org/10.2139/ssrn.4388560)
- **긍정, MBOFormer** (Schaurecker 등 2026): market-by-order(level-3) 이벤트 이력을 7,203개 파라미터의 causal transformer에 넣었다. 3개 시장 × 4개 horizon 12개 설정 중 11개에서 mean macro-F1이 가장 높았다. Apple M2 CPU 단일 스레드에서 추론 중앙값이 1 ms 미만이고, 직렬화 크기는 70 kB 미만이다(저자 보고) — [arXiv 2610.02917](https://arxiv.org/abs/2610.02917)
- 약한 긍정: S&P 500 10종목(2025-04~2026-03)의 topological anomaly score가 장중 수익 곡선에 예측 내용이 있다. 단독 저자이고 비용 언급이 없다 — [arXiv 2606.08586](https://arxiv.org/abs/2606.08586)
- (1차 조사에 이미 있음) TSFM의 random walk 대비 이득은 "small and sparse"였다 — [arXiv 2606.27100](https://arxiv.org/abs/2606.27100); FINSABER는 장기·다종목에서 LLM 우위가 약해졌다 — [arXiv 2505.07078](https://arxiv.org/abs/2505.07078)

### Inferences
- CZAR의 논점은 회귀 손실에 대한 것이지만, krx-quant의 3범주에도 같은 함정이 있다. flat이 다수이면 "항상 flat"이 정확도와 log-loss에서 강하다. 1차 보고서가 제안한 Gerrity score·kappa(#262)가 이 문제를 직접 다룬다.
- RetailAgent는 "LLM에게 장중 매매 판단을 맡기지 않는다"는 1차 결론을 강화한다.
- MBOFormer는 LOB가 수개월 쌓인 뒤 "작은 모델 + 이벤트 이력"이 2 GB 서버와 ONNX 추론에 맞는 구성임을 보여 준다(저자 보고뿐).

### Gaps
- 2025–2026년 KRX 분 단위 예측의 부정 결과나 재현 연구는 찾지 못했다.
- Aleti 등의 수치(Sharpe, 비용 가정)는 원문을 열지 못해 확인하지 못했다.

## krx-quant 적용 메모

조건: 3종목, 1분봉 약 1년, LOB는 수 주, 2 GB 서버에서 밤마다 재학습, 판단 기준은 up/flat/down hit rate. 아래는 비용 대비 기대값 순서다. 모두 사전에 정한 비교 기준(현재 Condorcet, 동일 가중, LightGBM 단독)을 리플레이에서 넘을 때만 넣는다.

| 순위 | 할 것 | 대상 | 비용 | 기대값 | 근거 |
|---|---|---|---|---|---|
| 1 | **구성원별 확률 보정 후 Condorcet 가중**: 최근 3~5일로 구성원마다 temperature 1개를 맞춘 뒤 log-loss 가중을 계산한다. class별 reliability(ECE)를 기록한다. isotonic은 class당 보정 표본이 1,000개를 넘을 때만 쓴다 | Condorcet, #262 | 거의 0 | 중간 | Guo 2017, Kull 2019, scikit-learn 문서 |
| 2 | **Condorcet 장중 online 가중**: log loss로 fixed-share EWA(= forgetting이 있는 BMA)를 돌리고, 정답이 확정될 때마다 갱신한다. 동일 가중 쪽 shrink를 같이 시험하고, 기준선에 동일 가중을 반드시 둔다 | Condorcet | 낮음 | 낮음~중간 | Herbster–Warmuth, Raftery 2010, combination puzzle |
| 3 | **Conformal 분류 set을 abstention 규칙으로**: APS/RAPS set에 ACI를 얹어 α를 온라인으로 맞춘다. set이 하나의 방향 class일 때만 신호를 낸다. 같은 리플레이에서 meta-labeling과 coverage–precision 곡선으로 비교한다 | #266 | 낮음 (MAPIE) | 중간 | Romano 2020, Angelopoulos 2020, Gibbs–Candès 2021 |
| 4 | **Adversarial validation drift 경보**: 밤마다 학습 창과 최근 1~2일을 구별하는 LightGBM을 돌려 AUC와 상위 drift 피처를 기록한다 | 운영 진단, #262 | 낮음 | 중간 (진단) | Pan 2020 |
| 5 | **Hayashi–Yoshida lead-lag 함수**: tick에서 선물→세 종목, 삼성전자↔SK하이닉스의 최적 lag와 장중 변화를 잰다. Granger·Kalman의 lag 선택과 피처로 쓴다. 미국 개장과 지표 발표 시각에 구조가 바뀌는지 본다 | Granger, Kalman | 낮음~중간 (LOB 수 주로 가능) | 중간 (피처), 낮음 (단독) | Hoffmann–Rosenbaum–Yoshida 2013, Huth–Abergel |
| 6 | **Signature 피처 실험**: 최근 10~30분의 (시간, log mid, 누적 OFI, 선물) 경로를 깊이 3으로 변환(39~120차원)해 Shannon logistic이나 LightGBM에 넣는다. Lévy area 항을 lead-lag 피처로 따로 본다 | Shannon, Turing/Breiman | 낮음 (iisignature) | 불확실 | Horvath 등 2026, Gyurkó 2013 |
| 7 | **Range 구간에 ACI 덧씌우기**: Mandelbrot HAR 구간을 그대로 두고 DtACI로 coverage만 맞춘다(HAR 대체 금지) | Mandelbrot | 낮음 | 낮음~중간 | Gibbs–Candès 2022, TCP 2025 |

**지금은 하지 않을 것**
- **GP·RL·LLM 자동 alpha mining**(AlphaGen, AlphaAgent, RD-Agent): cross-section IC를 목적함수로 하는 방법이라 3종목 시계열에 맞지 않는다. 독립 재현이 없고, 고정된 채점 함수에 과적합한다는 경고가 2026년에 나왔으며, LLM은 누설·비용 문제가 있다. 대신 AlphaAgent의 "기존 신호와의 유사도 제한·복잡도 제한"을 새 알고리즘 심사 규칙으로만 빌린다.
- **IRM·causal representation**: 일반 ML에서도 ERM을 넘지 못했고, 금융 근거는 저자 자체 결과뿐이다. PCMCI(tigramite)는 GPL-3.0 라이선스 문제가 있다.
- **GNN·DeltaLag·lead-lag 군집**: 종목이 3개라 그래프 구조가 없다.
- **OU 최적 진입 문턱(Bertram, Leung–Li)과 Kalman pairs hedge ratio**: Gauss가 κ ≈ 0을 배운 지금은 줄 것이 없다. κ > 0이 유의한 조건부 국면을 먼저 찾으면 그때 다시 본다. pairs 종류(삼성전자–SK하이닉스 상대강도)를 만들 때 Kalman hedge ratio를 재검토한다.
- **MBOFormer 류 소형 LOB transformer**: LOB가 수개월 쌓이면 다시 본다. 재평가 시점은 LOB 기록 일수를 조건으로 하는 예약 작업(`src/krx_quant/scheduled.py`)으로 둘 수 있다.
