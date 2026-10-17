# ML·딥러닝 단기 주가 예측 (tick~일봉, LLM/foundation model 포함)

기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다. "(미검증)"은 이번 조사에서 원문으로 확인하지 못한 내용이다. 연도가 오래된 결과는 연도를 붙여 표시했다. 논문 서지(제목, 저자, 학술지, 권, 쪽)는 arXiv API와 Crossref로 확인했다.

## 1. LOB 딥러닝: DeepLOB, FI-2010, TransLOB, DeepLOB-Attention, LOBCAST, Sirignano & Cont

### Takeaway
LOB 딥러닝은 FI-2010에서는 높은 점수를 내지만, 독립 benchmark(LOBCAST, 2024)에서는 15개 모델 모두 새 데이터에서 성능이 크게 떨어졌다. 예측이 맞는 horizon도 아주 짧아서 평균 가격 변화 2번 정도다. 신뢰할 만한 결과는 두 가지다. 여러 종목을 합쳐 학습하는 것이 낫고(Sirignano & Cont), raw book보다 stationary한 order flow(OFI) 입력이 낫다(Kolm 등).

### Cited Findings
- **FI-2010** (2018, 오래된 결과): Ntakaris, Magris, Kanniainen, Gabbouj, Iosifidis. "Benchmark dataset for mid-price forecasting of limit order book data with machine learning methods", *Journal of Forecasting* 37:852–866. LOB mid-price 예측의 첫 공개 benchmark다 — [arXiv 1705.03233](https://arxiv.org/abs/1705.03233), [DOI](https://doi.org/10.1002/for.2543). Nasdaq Nordic 5종목·10거래일 데이터라는 구성은 (미검증, 기억 기반).
- **DeepLOB** (2019, 오래된 결과): Zhang, Zohren & Roberts, *IEEE Trans. Signal Processing* 67(11):3001–3012. FI-2010과 London Stock Exchange 1년치 호가로 학습했다. 저자들은 학습에 없던 종목에도 잘 옮겨지고("translates well to instruments which were not part of the training set") 표본 밖 정확도가 안정적이라고 보고했다 — [arXiv 1808.03668](https://arxiv.org/abs/1808.03668), [DOI](https://doi.org/10.1109/TSP.2019.2907260)
- **TransLOB** (2020): Wallbridge, "Transformers for Limit Order Books". causal convolution 뒤에 masked self-attention을 둔 구조로, FI-2010에서 CNN·LSTM 계열을 넘는 SOTA를 주장했다(저자 보고) — [arXiv 2003.00130](https://arxiv.org/abs/2003.00130)
- **DeepLOB-Attention** (2021): Zhang & Zohren, "Multi-Horizon Forecasting for Limit Order Books…". encoder-decoder(seq2seq)에 Attention을 붙여 여러 horizon을 한 번에 예측한다. 짧은 horizon에서는 SOTA와 비슷하고 긴 horizon에서는 더 낫다. recurrent layer 때문에 학습이 느려서 Graphcore IPU로 가속했다 — [arXiv 2105.10430](https://arxiv.org/abs/2105.10430)
- **LOBCAST** (2024): Prata 등, "LOB-based deep learning models for stock price trend prediction: a benchmark study", *Artificial Intelligence Review* 57:116. 15개 모델(SOTA 13개, baseline 2개)을 FI-2010과, LOBSTER에서 새로 만든 LOB-2021·LOB-2022로 평가했다. 결론은 "all models exhibit a significant performance drop when exposed to new data"이다 — [arXiv 2308.01915](https://arxiv.org/abs/2308.01915), [DOI](https://doi.org/10.1007/s10462-024-10715-4)
  - DeepLOB의 F1: FI-2010 71.4±5.3(원 논문 주장 78.9±4.4) → LOB-2021 59.5±3.0, LOB-2022 59.5±2.9 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
  - BINCTABL의 F1: FI-2010 82.6±7.0 → LOB-2021 61.2±2.7, LOB-2022 59.2±3.3. horizon 평균으로 약 19.6% 떨어졌다 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
  - "All models are very sensitive to hyperparameters". hyperparameter 탐색 중 많은 run이 F1 ≤ 33%로 발산했다. 여러 모델의 seed 간 표준편차가 5점을 넘었다 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
  - Backtesting.py로 거래를 시뮬레이션한 결과, 수익성은 "far from guaranteed"였다 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
  - 원 논문 몇 편은 코드나 hyperparameter를 공개하지 않아 재구현이 필요했다 — [arXiv PDF](https://arxiv.org/pdf/2308.01915)
- **Sirignano & Cont** (2019, 오래된 결과): "Universal features of price formation in financial markets: perspectives from deep learning", *Quantitative Finance* 19:1449–1459. 미국 주식 호가·체결 수십억 건으로 학습했다. 모든 종목을 합쳐 학습한 universal model이 종목별 linear·nonlinear 모델보다 표본 밖에서 정확했고, 학습에 없던 종목에서도 통했다. 이 관계는 시간이 지나도 stationary했고, 더 긴 과거 이력이 예측을 개선했다(path-dependence) — [arXiv 1803.06917](https://arxiv.org/abs/1803.06917), [DOI](https://doi.org/10.1080/14697688.2019.1622295)
- **Deep OFI** (2023): Kolm, Turiel & Westray, *Mathematical Finance* 33(4):1044–1081. Nasdaq 115종목을 썼다. 흔한 신경망에 order flow(OFI) 입력을 주면 raw order book 입력 모델 대부분보다 크게 낫다. 종목별 예측의 유효 horizon은 "평균 가격 변화 약 2회"다. "information-rich"한 종목일수록 잘 맞는다 — [SSRN 3900141](https://papers.ssrn.com/abstract=3900141), [DOI](https://doi.org/10.1111/mafi.12413), [RePEc 요약](https://ideas.repec.org/a/bla/mathfi/v33y2023i4p1044-1081.html)
- **Lucchese, Pakkanen & Veraart** (2024): *International Journal of Forecasting* 40(4):1587–1621. 고빈도 mid-price 수익률의 예측력은 넓게 존재하지만, 성능은 order book 표현 방식에 크게 좌우된다("volume representation"을 제안). 이 예측력은 단독 전략으로는 거래 가능하지 않을 수 있다 — [arXiv 2211.13777](https://arxiv.org/abs/2211.13777), [RePEc](https://ideas.repec.org/a/eee/intfor/v40y2024i4p1587-1621.html)
- **Briola, Bartolucci & Aste** (2024, LOBFrame): "the stocks' microstructural characteristics influence the efficacy of deep learning methods", "high forecasting power does not necessarily correspond to actionable trading signals". 일반 ML metric으로는 예측 품질을 제대로 평가할 수 없다 — [arXiv 2403.09267](https://arxiv.org/abs/2403.09267)

### Inferences
- FI-2010 점수는 실전 성능의 지표가 아니다. 같은 모델도 새 시장·새 기간에서 F1이 60 안팎으로 내려간다. 3-class에서 무작위 기준선은 33이다.
- 예측력의 유효 horizon(가격 변화 ~2회)은 krx-quant의 분 단위 horizon보다 훨씬 짧다. 그래서 LOB 입력의 가치는 대부분 OFI 같은 요약 feature로 이미 잡힐 가능성이 크다. Shannon이 OFI를 쓰는 방향이 문헌과 맞다.
- 종목을 합쳐 학습하면 이득이라는 결과(Sirignano & Cont; DeepLOB의 transfer)는 데이터가 적은 3종목 엔진에 유리한 근거다.

### Gaps
- KRX(또는 아시아 시장) LOB에 DeepLOB 계열을 적용한 독립 검증은 찾지 못했다.
- DeepLOB가 학습에 필요한 최소 LOB 기간을 정량화한 연구는 찾지 못했다. LOBCAST 데이터셋의 정확한 기간도 이번에 확인하지 못했다.
- LOBCAST 부록의 수익 수치는 원문이 잘려서 읽지 못했다.

## 2. Cross-sectional ML: Gu-Kelly-Xiu, Virtue of Complexity, Chen-Pelger-Zhu, 차트 이미지 CNN

### Takeaway
월·주 단위로 수천 종목 cross-section을 다루는 ML은 근거가 가장 탄탄하다. 다만 이득은 nonlinear interaction과 소형주에서 나오고, 대형주로 좁히면 크게 줄어든다(이미지 CNN의 2차 출처). "complex model이 낫다"는 주장(KMZ)에는 2025년에 강한 반론(Nagel)이 나왔다. 3종목 분 단위 엔진에는 직접 옮길 수 없다.

### Cited Findings
- **Gu, Kelly & Xiu** (2020, 오래된 결과): "Empirical Asset Pricing via Machine Learning", *RFS* 33:2223–2273. 가장 잘한 방법은 trees와 neural networks이고, 그 이득은 다른 방법이 놓친 nonlinear predictor interaction에서 나온다. ML 예측을 쓰면 경제적 이득이 커서 "in some cases doubling the performance of leading regression-based strategies"였다. 모든 방법에서 지배적인 신호는 momentum, liquidity, volatility 계열로 같았다 — [DOI](https://doi.org/10.1093/rfs/hhaa009), [NBER w25398](https://www.nber.org/papers/w25398.pdf)
- **Kelly, Malamud & Zhou**: "The Virtue of Complexity in Return Prediction", *Journal of Finance* 79(1):459–503 (2024; 온라인 2023-12). 파라미터 수가 관측 수보다 많은 "complex" 모델이 단순 모델보다 수익률 예측력을 더 잘 드러낸다는 이론과, 미국 시장 수익률 예측에서의 실증을 냈다 — [DOI](https://doi.org/10.1111/jofi.13298)
- **반론, Nagel** (2025): "Seemingly Virtuous Complexity in Return Prediction" (Chicago Booth RP 25-10, NBER w34104). feature 수가 표본보다 훨씬 많으면 Random Fourier Features 예측은 과거 수익률의 가중평균이 된다. 짧은 window에서는 이것이 volatility-timed momentum과 같아진다. 반전(reversal)이 있는 인공 데이터에서도 같은 방법이 momentum 전략을 만들고 성적이 나빴다 — [SSRN 5335012](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5335012), [NBER PDF](https://www.nber.org/system/files/working_papers/w34104/w34104.pdf)
  - Fed FEDS 2025-089(환율)는 Ridge-RFF의 이점이 표본이 커질수록 사라진다고 보고했다 — [FEDS PDF](https://www.federalreserve.gov/econres/feds/files/2025089pap.pdf) (검색 요약 기반, 본문 미확인)
- **Chen, Pelger & Zhu**: "Deep Learning in Asset Pricing", *Management Science* 70:714–750 (2024). no-arbitrage 조건을 목적함수로 쓰고, adversarial 방식으로 가장 정보가 많은 test asset을 만든다. 표본 밖에서 Sharpe ratio, explained variation, pricing error 모두 benchmark를 넘었다고 보고했다 — [arXiv 1904.00745](https://arxiv.org/abs/1904.00745), [DOI](https://doi.org/10.1287/mnsc.2023.4695). 구체적 Sharpe 수치는 (미검증).
- **Jiang, Kelly & Xiu**: "(Re-)Imag(in)ing Price Trends", *Journal of Finance* (2023). 주가 차트 이미지를 CNN에 넣어 찾은 패턴은 기존 trend 신호와 대체로 다르고, 더 정확하고, 수익성도 높았다 — [SSRN 3756587](https://papers.ssrn.com/abstract=3756587), [DOI](https://doi.org/10.1111/jofi.13268)
  - 2차 출처(BSIC 블로그)에 따르면, equal-weight long-short의 연 Sharpe는 5/20/60일 모델에서 7.2/6.8/4.9였고 상위 500 대형주로 좁히면 1을 약간 넘는 수준으로 떨어졌다 — [BSIC](https://bsic.it/technical-analysis-on-steroids/) (원문 표 미검증)

### Inferences
- 이 연구들은 수천 종목의 순위(cross-sectional ranking)를 예측하는 문제다. 3종목의 방향(time-series)을 예측하는 krx-quant와는 문제가 다르다. 옮길 만한 교훈은 두 가지다. 이득은 nonlinear interaction에서 나오고(GBDT도 잡는다), momentum·liquidity·volatility feature가 핵심이다.
- 이미지 CNN의 높은 Sharpe가 대형주에서 크게 줄었다는 보고(미검증)는 삼성전자·SK하이닉스 같은 초대형주에 기대치를 낮춰야 한다는 신호다.
- Nagel의 비판은 "큰 모델이 좋아 보이면, 실제로 배운 것이 변동성으로 조절한 momentum 아닌가"를 점검해야 한다는 뜻이다.

### Gaps
- GKX의 tree·NN out-of-sample R² 정확한 수치는 이번에 확인하지 못했다.
- 한국 시장에서 GKX 방식을 재현한 연구는 이번 조사 범위에서 찾지 못했다.

## 3. 라벨링과 학습 관행: triple-barrier, meta-labeling, fractional differentiation, uniqueness weight, mid-price label, flat class

### Takeaway
LOB 문헌의 표준 label은 "다음 k개 mid-price 평균과 현재 mid-price의 차이 + 고정 임계값 θ"다. 그런데 θ가 고정되면 horizon마다 class 비율이 크게 달라지고, flat이 다수인 구간에서는 정확도가 부풀려진다. López de Prado의 방법들(triple-barrier, meta-labeling, uniqueness weight, purged CV)은 겹치는 label과 누설 문제에 대한 표준 처방이다.

### Cited Findings
- LOBCAST의 label 정의: 다음 k개 mid-price의 평균 a⁺(k,t)를 현재 m(t)와 비교한다. a⁺ > m(t)(1+θ)이면 Up, a⁺ < m(t)(1−θ)이면 Down, 나머지는 Stable이다. k ∈ {1,2,3,5,10} — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
- FI-2010은 모든 horizon에 θ = 0.002를 쓴다. class가 균형인 것은 k = 5뿐이다. k = 1에서는 U 18% / S 63% / D 19%, k = 10에서는 U 37% / S 25% / D 38%다. LOBCAST는 FI-2010 labelling이 불안정하고(Zhang 등 인용), horizon별로 불균형하며, 원 LOB를 되짚을 수 없게 전처리됐다고 비판했다 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
- θ를 고정하면 변동성이 큰 종목이 기준을 지배해서, 변동성이 작은 종목은 대부분 Stable로 찍힌다 — [arXiv HTML](https://arxiv.org/html/2308.01915v2)
- Briola 등은 일반 ML metric 대신 "완결된 거래를 맞게 예측할 확률"로 평가하자고 제안했다 — [arXiv 2403.09267](https://arxiv.org/abs/2403.09267)
- López de Prado, *Advances in Financial Machine Learning* (Wiley, 2018, 오래된 결과) — [Wiley](https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086). 이번 조사에서 책 본문은 다시 확인하지 않았다. 아래는 널리 알려진 내용의 요약이다.
  - triple-barrier: 변동성에 맞춘 이익실현 barrier, 손절 barrier, 시간 barrier 중 먼저 닿는 것으로 label을 정한다.
  - meta-labeling: 1차 모델이 방향을 정하면, 2차 모델이 "이 신호가 맞을지"(베팅 여부와 크기)를 학습한다. 신호를 덜 내는 대신 precision을 올리는 용도다.
  - uniqueness 기반 sample weight: label 기간이 겹치는 표본은 정보가 중복되므로 가중치를 낮춘다(sequential bootstrap도 같은 목적).
  - fractional differentiation: 정수 차분 대신 분수 차분으로, stationarity를 얻으면서 기억(memory)을 최대한 남긴다.
  - purged k-fold CV + embargo: label 기간이 test와 겹치는 train 표본을 지우고, test 직후 구간을 비워서 누설을 막는다.

### Inferences
- krx-quant는 1분봉에서 몇 분 앞을 label로 쓴다. 그래서 이웃한 label이 여러 bar씩 겹친다. uniqueness weight와 purge/embargo가 이론상 바로 적용되는 상황이다.
- 사용자가 up/flat/down hit rate로 판단한다면, 전체 정확도보다 "up이라고 한 것의 적중률"과 "down이라고 한 것의 적중률"(class별 precision), 그리고 신호를 낸 비율(coverage)을 같이 보는 편이 FI-2010식 함정(flat 다수 → 정확도 부풀림)을 피한다.
- 고정 θ 대신 종목별 변동성이나 호가 단위(tick)·스프레드에 비례하는 θ가 LOBCAST의 비판과 맞는다.
- meta-labeling은 "정확도 우선"이라는 사용자 기준에 가장 직접 맞는 도구다. 신경망을 추가하는 것보다 싸고, 기존 Turing·Breiman 위에 얹을 수 있다.

### Gaps
- mid-price smoothing window(k)를 바꿨을 때 실제 거래 가능성이 어떻게 변하는지 정량 비교한 연구는 이번에 확인하지 못했다.
- 분 단위 주식 데이터에서 fractional differentiation이 성능을 올린다는 독립 실증은 찾지 못했다.

## 4. Gradient boosting vs 신경망 (tabular)

### Takeaway
일반 tabular 데이터에서는 표본이 1만 개 안팎인 중간 규모일 때 tree 모델이 여전히 우세하다(Grinsztajn 2022). 금융에서도 범용 foundation model이 LightGBM·CatBoost에 지는 결과가 있다. 다만 미국 월간 cross-section(GKX)에서는 NN이 tree와 비슷하거나 약간 낫다. 고빈도에서는 입력 표현(OFI)이 모델 구조보다 중요하다는 결과가 일관된다.

### Cited Findings
- Grinsztajn, Oyallon & Varoquaux (2022): "Why do tree-based models still outperform deep learning on tabular data?". "medium-sized data (~10K samples)"에서 tree가 SOTA다. 이유는 inductive bias 차이다. 신경망은 uninformative feature에 약하고, 데이터 방향(orientation)을 보존하지 않으며, 불규칙한 함수를 잘 배우지 못한다 — [arXiv 2207.08815](https://arxiv.org/abs/2207.08815). NeurIPS 2022 Datasets & Benchmarks 게재로 알려져 있으나 이번에 확인하지 못했다(미검증).
- GKX (2020): trees와 NN이 최상위이고 이득은 nonlinear interaction에서 나온다 — [DOI](https://doi.org/10.1093/rfs/hhaa009)
- Rahimikia 등 (2025)의 관련 버전(94개국, 34년)에서는 zero-shot Chronos·TimesFM이 CatBoost·LightGBM ensemble보다 못했다 — [arXiv 2511.18578](https://arxiv.org/abs/2511.18578) (검색 요약 기반, 본문 미확인)
- Kolm 등 (2023): stationary OFI 입력에 흔한 신경망을 쓰는 것으로 SOTA 정확도를 냈다 — [RePEc](https://ideas.repec.org/a/bla/mathfi/v33y2023i4p1044-1081.html)

### Inferences
- 거래일 250일 × 정규장 390분으로 계산하면, krx-quant의 학습 window는 Turing(60일)이 종목당 약 2.3만 bar, Breiman(20일)이 약 7.8천 bar다. Grinsztajn이 말한 "tree가 이기는" 규모 그대로다. 3종목을 합쳐도 10만 단위 미만이다.
- 따라서 신경망을 LightGBM의 "대체"로 기대하면 안 된다. 기대할 수 있는 것은 오차 상관이 낮은 ensemble 구성원으로서의 작은 이득이다.

### Gaps
- 같은 feature로 분 단위 주식 방향 예측에서 LightGBM과 신경망을 정면 비교한, 신뢰할 만한 금융 특화 연구는 이번 조사에서 찾지 못했다.

## 5. Time-series foundation model과 LLM

### Takeaway
범용 TSFM(TimesFM, Chronos, Moirai)은 금융 수익률에서 zero-shot과 fine-tuning 모두 약하다는 독립 결과가 나왔다. 금융 특화 모델(Kronos, FinCast)은 저자 자체 평가만 있다. LLM 뉴스 감성은 knowledge cutoff 이후 표본에서도 예측력이 보고되지만, 주로 거래가 불가능한 초기 반응과 소형주·악재 drift에 몰려 있고, LLM 확산과 함께 수익이 줄고 있다. look-ahead와 memorization 오염은 2025–2026년 연구의 핵심 쟁점이다.

### Cited Findings
- 범용 TSFM 원 논문: TimesFM (Das 등, 2023) — [arXiv 2310.10688](https://arxiv.org/abs/2310.10688); Chronos (Ansari 등, 2024) — [arXiv 2403.07815](https://arxiv.org/abs/2403.07815); Moirai (Woo 등, 2024) — [arXiv 2402.02592](https://arxiv.org/abs/2402.02592)
- **Rahimikia, Ni & Wang** (2025-11): "Re(Visiting) Time Series Foundation Models in Finance". 일별 초과수익률 대규모 데이터에서 off-the-shelf TSFM은 "perform poorly in zero-shot and fine-tuning settings"였다. 금융 데이터로 처음부터 사전학습한 모델은 큰 개선을 보였다 — [arXiv 2511.18578](https://arxiv.org/abs/2511.18578)
  - 검색 요약에 따르면 TimesFM(20M)의 연 수익률이 입력 길이 5일 때 −18.22%, 512일 때 30.36%로, 설정에 따라 크게 흔들렸다 — [arXiv 2511.18578](https://arxiv.org/abs/2511.18578) (본문 미확인)
- **Noguer i Alonso & Franklin** (2026-06): 미국 5종목(AAPL, AMZN, GOOG, JPM, META)에서 TimeGPT, TimesFM-2.5, Moirai-2.0, Chronos(-2)를 NBEATS, NHITS, PatchTST, iTransformer, KAN과 비교했다. TSFM이 10개 task 중 8개에서 순위 1위였지만 "gains over the random-walk benchmark are small and sparse"였다. Diebold-Mariano 검정이 유의한 것은 2건(Chronos-AMZN, Moirai-2.0-GOOG)뿐이었다. 결론은 데이터가 적을 때 쓸 만한 prior이지만 "not universal engines for statistically reliable alpha generation"이다 — [arXiv 2606.27100](https://arxiv.org/abs/2606.27100)
- **Brini** (2026-07): realized volatility 예측에서 zero-shot으로 Log-HAR을 모든 horizon에서 이긴 것은 가장 작은 TTM(<1M 파라미터)뿐이었고, 차이도 약 1.3–1.8%였다 — [arXiv 2607.05291](https://arxiv.org/abs/2607.05291) (검색 요약 기반)
- **Kronos** (Shi 등, Tsinghua, 2025-08, preprint): 45개 거래소의 K-line 120억+ 건으로 사전학습했다. K-line 전용 tokenizer와 decoder-only Transformer를 쓴다. 저자 보고로는 가격 예측 RankIC가 최고 TSFM 대비 +93%, 사전학습 없는 최고 모델 대비 +87%였고, 변동성 MAE는 9% 낮았다. 저자들도 범용 TSFM이 K-line에서 사전학습 없는 모델보다 못한 경우가 많다고 썼다. MIT 라이선스로 공개됐다 — [arXiv 2508.02739](https://arxiv.org/abs/2508.02739), [HF papers](https://huggingface.co/papers/2508.02739). 독립 재현은 찾지 못했다.
- **FinCast** (Zhu 등, 2025-08): 금융 특화 foundation model로 zero-shot SOTA를 주장한다(저자 보고). 저자들은 범용 모델이 benchmark와 사전학습 데이터가 겹쳐서 이득을 봤을 수 있다고 경고했다 — [arXiv 2508.19609](https://arxiv.org/abs/2508.19609)
- **Lopez-Lira & Tang**, "Can ChatGPT Forecast Stock Price Movements?" (버전마다 주장이 다르다). v6(2025-10)는 knowledge cutoff 이후 헤드라인을 썼다. GPT-4는 거래할 수 없는 초기 반응에서 portfolio-day hit rate 약 90%를 보였고, 이후 drift도 예측했다(특히 소형주·악재). 모델이 클수록 예측력이 높았지만, LLM 채택이 늘면서 전략 수익이 줄었다 — [arXiv 2304.07619](https://arxiv.org/abs/2304.07619) (v6 내용은 검색 요약 기반)
- **Glasserman & Lin** (2023): LLM 학습 기간 안(in-sample)에서는 회사 이름을 지운 헤드라인이 더 잘 맞았다. 대형주에서 특히 그랬다. 즉 "distraction effect"(회사에 대한 일반 지식이 감성 판단을 방해)가 look-ahead bias보다 컸다. 표본 밖에서는 look-ahead는 문제가 아니지만 distraction은 남는다 — [arXiv 2309.17322](https://arxiv.org/abs/2309.17322)
- **Gao, Jiang & Yan** (2025-12): "Detecting Lookahead Bias in LLM Forecasts". 뉴스 없이 날짜만 주는 recall prompt로 "Lookahead Propensity"(LAP)를 잰다. LAP는 학습 기간 안에서 양수이고 cutoff 직후 거의 0이 됐다. LAP가 높은 표본에서 LLM 예측력이 커졌다 → 백테스트 성과의 일부는 기억이다 — [arXiv 2512.23847](https://arxiv.org/abs/2512.23847)
- 회사명 마스킹은 부분 처방일 뿐이다. LLM이 최소한의 맥락에서도 회사와 날짜를 복원한다는 보고가 있다 — [arXiv 2504.14765](https://arxiv.org/html/2504.14765v2) (검색 요약 기반, 저자·서지 미검증)
- **FINSABER** (Li, Kim, Cucuringu, Ma, 2025): 2000–2024년, 100개 넘는 종목으로 평가했더니 기존에 보고된 LLM 전략의 우위가 크게 약해졌다. 상승장에서는 너무 보수적이고 하락장에서는 너무 공격적이었다 — [arXiv 2505.07078](https://arxiv.org/abs/2505.07078)
- **FinGPT** (Yang, Liu, Wang, 2023): 오픈소스 금융 LLM framework다. 이번 조사에서 엄격한 표본 밖 거래 성과는 확인하지 못했다 — [arXiv 2306.06031](https://arxiv.org/abs/2306.06031)

### Inferences
- 범용 TSFM을 수익률 예측기로 그대로 쓰는 것은 근거가 약하다. 금융 특화 모델(Kronos)은 시도해 볼 만하지만 저자 주장뿐이다.
- Kronos는 2025-08 공개라서, krx-quant의 1분봉 1년치(대략 2025-10 ~ 2026-10) 대부분은 공개 이후 구간이다. 그래도 사전학습 데이터의 마지막 날짜는 이번에 확인하지 못했으므로 오염 가능성을 완전히 배제할 수 없다.
- LLM 감성의 강한 예측력은 "초기 반응"에 있는데, 이 구간은 거래가 불가능하다고 저자들 스스로 썼다. 분 단위 엔진에서 뉴스를 읽고 들어가면 이미 늦을 가능성이 크다. 백테스트는 반드시 모델 cutoff 이후 구간에서만 해야 한다.

### Gaps
- 한국어 뉴스나 KRX 종목에 대한 LLM 감성 예측의 엄격한 표본 밖 연구는 찾지 못했다.
- Kronos 모델 크기별 파라미터 수와 추론 비용은 이번에 확인하지 못했다.
- LLM agent의 실거래(paper trading이 아닌) 성과에 대한 신뢰할 만한 기록은 찾지 못했다.

## 6. 강화학습(RL) 트레이딩: 근거의 질

### Takeaway
RL 트레이딩의 근거는 대부분 저자가 설계한 백테스트다. 리뷰들은 비현실적 설정, 실시간 검증 부재, overfitting 위험을 반복해서 지적한다. RL은 신호를 예측하는 도구가 아니라 포지션·실행 정책을 학습하는 도구라서 signals-only 엔진과는 목적도 다르다.

### Cited Findings
- Pricope (2021) 리뷰: 저빈도 주식 DRL 연구 다수가 "proof-of-concept" 수준이었고 비현실적 설정에서 실험했다. 기준 전략보다 통계적으로 낫다는 결과는 많았지만 "no decent profitability level"이었고, 실시간 온라인 검증도 없었다 — [arXiv 2106.00123](https://arxiv.org/abs/2106.00123)
- Gort 등 (2022, 암호화폐): 기존 DRL 백테스트는 overfitting에 따른 false positive로 수익이 낙관적으로 보고됐을 수 있다. overfitting 탐지를 가설검정으로 다뤄 과적합된 agent를 걸러내자고 제안했다 — [arXiv 2209.05559](https://arxiv.org/abs/2209.05559)
- Zhang, Zohren & Roberts (2019, 오래된 결과): 2011–2019년 유동성 상위 선물 50종에서 DRL이 time-series momentum보다 낫고, 높은 거래비용에도 수익이 났다고 보고했다(저자 보고, 선물·일 단위) — [arXiv 1911.10107](https://arxiv.org/abs/1911.10107)
- 개관 자료: Hambly, Xu & Yang, "Recent Advances in Reinforcement Learning in Finance" — [arXiv 2112.04553](https://arxiv.org/abs/2112.04553). 오픈소스 라이브러리 FinRL — [arXiv 2011.09607](https://arxiv.org/abs/2011.09607)

### Inferences
- up/flat/down 신호만 내는 krx-quant에서 RL은 해결하려는 문제가 아니다. 데이터가 적은 상황에서는 overfitting 위험만 커진다.

### Gaps
- 주식 DRL에 대해 독립적인 대규모 재현 연구(LOBCAST나 FINSABER 같은)는 이번 조사에서 찾지 못했다.

## 7. 2023–2026 최근 결과와 negative result 요약

### Takeaway
최근 3년간 가장 믿을 만한 결과는 대부분 "재현해 보니 약하다"는 쪽이다. LOB 딥러닝은 새 데이터에서 무너졌고(LOBCAST), 범용 TSFM은 random walk를 거의 못 이겼다. LLM 전략은 기간을 늘리면 우위가 사라졌고(FINSABER), complexity의 이점은 volatility-timed momentum으로 설명됐다(Nagel). LLM 백테스트는 기억 오염이 측정 가능하다(LAP).

### Cited Findings
- LOB DL 15개 모델 모두 새 데이터에서 성능이 크게 떨어졌다 — [arXiv 2308.01915](https://arxiv.org/abs/2308.01915)
- 높은 예측력이 곧 거래 가능한 신호는 아니다 — [arXiv 2403.09267](https://arxiv.org/abs/2403.09267); 단독 전략으로 거래 가능하지 않을 수 있다 — [arXiv 2211.13777](https://arxiv.org/abs/2211.13777)
- TSFM의 random walk 대비 이득은 작고 드물다 — [arXiv 2606.27100](https://arxiv.org/abs/2606.27100); off-the-shelf TSFM은 zero-shot과 fine-tuning 모두 부진했다 — [arXiv 2511.18578](https://arxiv.org/abs/2511.18578)
- LLM 전략은 장기·다종목에서 우위가 약해졌다 — [arXiv 2505.07078](https://arxiv.org/abs/2505.07078); LLM 채택이 늘수록 수익이 줄었다 — [arXiv 2304.07619](https://arxiv.org/abs/2304.07619)
- "Virtue of complexity"는 volatility-timed momentum으로 설명된다는 반론 — [SSRN 5335012](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5335012)
- LLM 예측력의 일부는 knowledge cutoff 이전 정보의 기억이다 — [arXiv 2512.23847](https://arxiv.org/abs/2512.23847)

### Inferences
- 새 모델을 도입할 때 기준은 "논문 점수"가 아니라 krx-quant 자체의 walk-forward 표본 밖 hit rate와 log-loss여야 한다. 사전에 정한 비교 기준(LightGBM 단독)을 넘지 못하면 넣지 않는 규칙이 문헌과 맞다.

### Gaps
- 2026년 발표 논문 가운데 KRX 분 단위 데이터를 쓴 ML/DL 연구는 이번 조사에서 찾지 못했다.

## krx-quant 적용 메모

조건: 1분봉 1년치, LOB는 2026-10 초부터 수 주뿐, 학습 서버 2 GB(학습은 EC2 Spot으로 옮길 수 있음, AWS 월 $100 상한), 추론은 가벼워야 함(ONNX Runtime), 판단 기준은 up/flat/down hit rate.

**해볼 가치가 있는 것 (우선순위 순)**
1. **label·평가 정비** (비용 거의 0). flat 임계값을 고정값에서 종목별 변동성·스프레드 비례로 바꾼다. class별 precision(up 적중률, down 적중률)과 coverage를 같이 기록한다. 겹치는 label에 uniqueness weight를 주고 purge/embargo walk-forward로 검증한다. 근거: LOBCAST의 고정 θ 비판, López de Prado.
2. **Turing·Breiman 위에 meta-labeling**. 2차 LightGBM이나 logistic이 "1차 신호가 맞을지"를 예측해서 신호를 거른다. coverage를 내주고 hit rate를 올리는, 사용자 기준에 가장 맞는 수단이다. 서버 부담도 거의 없다.
3. **작은 신경망 1개를 Condorcet의 구성원으로**. 3종목을 합쳐 학습한다(Sirignano & Cont). 최근 30–60개 1분봉 + 기존 feature를 받는 작은 MLP나 1D-CNN/GRU 정도로 한다. CPU로 밤마다 학습할 수 있는 크기로 두고, 필요하면 Spot에서 학습한 뒤 ONNX로 내보낸다. 기대치는 "LightGBM 대체"가 아니라 오차 상관이 낮은 구성원으로서의 작은 이득이다(Grinsztajn: 학습 window 7.8천~2.3만 bar는 tree가 강한 규모). walk-forward log-loss와 class별 hit rate가 LightGBM 단독을 넘지 못하면 넣지 않는다.
4. **(낮은 우선순위) Kronos zero-shot을 오프라인에서 한 번 시험**. 3종목 1분봉에서 Kronos의 예측값을 LightGBM feature로 넣어 본다. 근거는 저자 보고뿐이다. 사전학습 데이터 기간을 확인하기 전까지는 오염 가능성을 적어 둔다.

**지금은 하지 않을 것**
- **DeepLOB, TransLOB, DeepLOB-Attention 같은 raw LOB 딥러닝**: LOB 이력이 수 주라 학습량이 턱없이 부족하다. 데이터가 많은 연구에서도 새 데이터에서 F1이 ~60으로 떨어졌다(LOBCAST). 예측 horizon(가격 변화 ~2회)도 엔진의 분 단위 horizon보다 짧다. LOB가 수개월 쌓이면 다시 본다. 그때도 raw book 대신 OFI 같은 stationary 입력에 작은 모델을 쓴다(Kolm 등). 재평가 시점은 LOB 기록 일수를 조건으로 하는 예약 작업(`src/krx_quant/scheduled.py`)으로 두면 된다.
- **범용 TSFM(Chronos, TimesFM, Moirai)으로 수익률 예측**: 금융 수익률에서 zero-shot과 fine-tuning 모두 약하고, random walk 대비 이득도 작고 드물다.
- **LLM 뉴스 감성과 LLM agent**: 강한 예측력은 거래할 수 없는 초기 반응에 몰려 있고, 장기 평가에서는 우위가 사라졌다. 분 단위 엔진에 붙이려면 한국어 실시간 뉴스와 비용이 들고, 백테스트는 오염된다. 필요하면 나중에 일 단위 "이벤트 있음" 플래그 정도로만 검토한다.
- **RL**: 근거의 질이 낮고, signals-only 엔진과 목적이 다르다.
- **Cross-sectional ML(GKX, Chen-Pelger-Zhu, 차트 이미지 CNN)과 과대 파라미터 "complex" 모델**: 수천 종목을 월·주 단위로 다루는 방법이라 3종목에는 맞지 않는다. 대형주에서는 성과가 크게 준다는 보고(미검증)가 있고, complexity의 이점은 Nagel의 반론을 받았다.
