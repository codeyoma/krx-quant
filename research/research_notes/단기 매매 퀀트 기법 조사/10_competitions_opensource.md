# 시장 예측 대회 상위 해법과 단기 전략 연구용 오픈소스 도구

> 조사일 2026-10-17. 연구 노트이며 투자 조언이 아니다.
> 출처: 대회 개요·평가·데이터 페이지와 Kaggle 토론글 원문(Kaggle 사이트에서 직접 읽음). 저장소 수치(stars, licence, 마지막 commit, release)는 같은 날 GitHub API와 PyPI JSON에서 가져왔다.
> 조사 중에 web search 한도가 다 차서 그 뒤로는 알려진 URL만 직접 읽었다. 그래서 찾지 못한 것(대회 1위 write-up 일부, 한국 퀀트 커뮤니티)은 Gaps에 적었다.

## 1. 대회별 과제·데이터·horizon·지표·상위 해법

### Takeaway
상위 해법은 대개 단순한 모델(LightGBM 하나, 또는 GBDT+NN 블렌드)을 썼다. 승부는 세 가지에서 갈렸다. 시장 전체 대비 상대 피처(cross-sectional mean/median/rank)를 만들었는지, 시간 순서를 지킨 purged/embargo CV를 했는지, 정답이 공개되는 대회에서는 online learning(자주 다시 학습)을 했는지다. 1분~1일 horizon 대회 대부분에서 상위권은 운의 영향을 크게 받았다고 우승자들이 직접 말한다.

### 대회 요약표

| 대회 (기간) | 과제 / 데이터 | Horizon | 평가 지표 | 팀 수 |
|---|---|---|---|---|
| [Optiver Realized Volatility Prediction](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction) (2021-06 ~ 2022-01) | 주식 10분치 book/trade 데이터로 다음 10분 realized volatility 예측 | 10분 | RMSPE | 3,852 |
| [Optiver – Trading at the Close](https://www.kaggle.com/competitions/optiver-trading-at-the-close) (2023-09 ~ 2024-03) | Nasdaq 장 마감 10분 closing auction(imbalance, reference/near/far price, top-of-book, WAP). 예측 대상은 종목 WAP의 60초 뒤 변화에서 synthetic index 변화를 뺀 값(bp) | 60초 | MAE | 4,436 |
| [Jane Street Market Prediction](https://www.kaggle.com/competitions/jane-street-market-prediction) (2020-11 ~ 2021-08) | 익명 feature 130개. 거래마다 action 0/1. 학습용으로 여러 horizon의 resp_1~4 제공 | 비공개(여러 horizon) | Utility(Sharpe 비슷한 t를 0~6으로 자르고 Σp를 곱함) | 4,245 |
| [Jane Street Real-Time Market Data Forecasting](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting) (2024-10 ~ 2025-07) | 익명 feature 79개, responder 9개. responder_6 예측. API가 timestep 단위로 데이터를 주고, 전날 responder는 lags로 제공 | 비공개 | sample-weighted zero-mean R² | 3,757 |
| [G-Research Crypto Forecasting](https://www.kaggle.com/competitions/g-research-crypto-forecasting) (2021-11 ~ 2022-05) | 코인 14종 1분 OHLCV·VWAP·Count | 15분 residualized return | weighted Pearson corr | 1,946 |
| [Ubiquant Market Prediction](https://www.kaggle.com/competitions/ubiquant-market-prediction) (2022-01 ~ 2022-07) | 중국 주식 수천 종목, 익명 feature 300개, 난독화된 target | 비공개 | time_id별 Pearson 평균 | 2,893 |
| [JPX Tokyo Stock Exchange Prediction](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction) (2022-04 ~ 2022-10) | 일본 주식 약 2,000종목 순위 매기기. options, financials, trades 데이터 제공 | 종가 t+1→t+2 | 상위 200 매수·하위 200 매도 daily spread return의 Sharpe | 2,033 |
| [DRW – Crypto Market Prediction](https://www.kaggle.com/competitions/drw-crypto-market-prediction) (2025-05 ~ 2025-07) | 크립토 1분 데이터. bid/ask_qty, buy/sell_qty, volume과 익명 X1~X780. test의 timestamp는 섞임 | 비공개 | Pearson corr | 1,091 |
| [Hull Tactical – Market Prediction](https://www.kaggle.com/competitions/hull-tactical-market-prediction) (2025-09 ~ 2026-06) | S&P 500 일별 데이터. 매일 0~2 배율의 비중을 냄. feature 그룹은 M/E/I/P/V/S/MOM/D | 1일 | 변동성 제약(시장의 120%)을 둔 Sharpe 변형 | 3,677 |
| [MITSUI&CO. Commodity Prediction Challenge](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge) (2025-07 ~ 2026-01) | LME, JPX, 미국 주식, FX. target 424개(단일 종목 log return 또는 두 종목 차이). lag 1~4 label 제공 | 일 단위(lag 1~4) | Spearman 평균 ÷ 표준편차(Sharpe 변형) | 1,711 |
| [Numerai Tournament](https://docs.numer.ai/) (상시 운영) | 주식 point-in-time feature(v6에서 0~4로 binning한 numeric 1,017개). era 단위. target은 market/sector 등을 제거한 residual return | 20일, 60일(`target`은 `target_jupiter_60`의 별칭) | CORR, MMC | — |

출처: 각 대회 Overview/Evaluation/Data 페이지(위 링크), 팀 수와 기간은 Kaggle 대회 메타데이터. Optiver RV의 10분 target은 [Data 페이지](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/data). Mitsui target 424개는 [Data 페이지](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/data). Numerai는 [docs](https://docs.numer.ai/)와 [data docs](https://docs.numer.ai/numerai-tournament/data).

### Cited Findings

**Optiver Realized Volatility (2021)**
- 1위 nyanp(공개 LB 2위에서 최종 1위). 요약하면 세 가지다. (1) tick size로 정규화 전 실제 가격을 되찾고 t-SNE로 time_id 순서를 복원해 time-series CV(4-fold)를 했다. (2) Nearest neighbour aggregation feature로 점수가 0.21에서 0.19로 좋아졌다. (3) LightGBM, MLP, 1D-CNN을 블렌드했다. 피처 약 600개 중 360개가 NN 피처였다. adversarial validation에서 시간에 따라 변하는 피처(order_count, total_volume)는 time_id 안의 rank로 바꿨다. — [1st Place Solution – Nearest Neighbors](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/discussion/274970)
- 1위 해법에서 효과가 없었던 것: beta 같은 도메인 피처, TabNet, residual에 학습한 NN, 차원 축소 피처. — [같은 글](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/discussion/274970)
- 1위 본인의 ablation 결과. LightGBM 하나만으로도 1위 점수가 나왔다(private 0.19699). tick-size leak을 빼면 최고 0.20367, time_id 사이 관계를 빼면 0.22018이었다. rank normalization은 효과가 거의 없었다(0.19699 vs 0.19708). 결론은 "모델 다양성보다 피처 다양성"이었다. — [Ablation study](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/discussion/302626)
- 3위(잠정, 공개 6위)도 time_id nearest neighbour를 썼다. 여기에 target을 직전 600초 RV에 대한 비율(target/RV₀₋₆₀₀)로 바꿔 비정상성(non-stationarity)을 줄였다. 600초를 반으로 나눈 "300초 모델"로 test 데이터까지 학습에 썼고, 시장 평균 변동성 예측값(macro estimation)을 피처로 넣었다. 모델은 LightGBM×ANN stacking이다. — [3rd Place](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/discussion/278676)

**Optiver Trading at the Close (2023)**
- 1위 hyd. CatBoost 0.5, GRU 0.3, Transformer 0.2 가중 블렌드이고 세 모델이 같은 300개 피처를 쓴다. 피처는 raw price, mid, imbalance, rolling, 과거 target, `seconds_in_bucket` 구간(0~300/300~480/480~)별 first-ratio와 expanding mean, 같은 초 기준 종목 간 rank와 mean ratio다. 검증은 앞 400일 학습, 마지막 81일 holdout이었다. online learning으로 12일마다 다시 학습했고(총 5회), post-processing으로 예측값에서 종목 가중치(synthetic index 비중)로 가중한 평균을 뺐다. 단순 평균보다 나았다. — [1st place solution](https://www.kaggle.com/competitions/optiver-trading-at-the-close/discussion/487446)
- 같은 글에서 online learning 효과(CatBoost test MAE): 없음 5.4523, 1회 5.4291, 5회 5.4165. 메모리를 아끼려고 학습 데이터를 하루 단위 파일로 저장하고 미리 할당한 float32 배열에 하루씩 읽었다. 효과가 없었던 것: 1D-CNN/MLP 앙상블, 여러 날을 넣은 GRU 입력, 더 큰 transformer, GBDT로 target bucket 평균 예측. — [같은 글](https://www.kaggle.com/competitions/optiver-trading-at-the-close/discussion/487446)
- 6위는 seq2seq Transformer 3개와 GRU 1개다. 종목 축 attention을 쓰고, 매일 새 데이터로 incremental update를 했다. 출력 합이 0이 되도록 제약을 걸었다. — [6th Place](https://www.kaggle.com/competitions/optiver-trading-at-the-close/discussion/486040)
- 7위는 LightGBM과 LSTM/ConvNet을 함께 썼다. 같은 (date, seconds_in_bucket) 안에서 median과의 차이(deviation) 피처와 online learning이 "오차를 크게 줄였다"고 한다. — [7th place](https://www.kaggle.com/competitions/optiver-trading-at-the-close/discussion/486169)

**Jane Street Market Prediction (2020–21)**
- 1위 Yirun Zhang 팀. Supervised Autoencoder와 MLP를 fold마다 함께 학습하고 팀원의 XGBoost와 블렌드했다. autoencoder를 CV 밖에서 따로 학습하면 validation fold를 이미 본 셈이 되어 label leakage가 생긴다고 지적했다. 그 밖에 5-fold 31-gap purged group time-series split, 분산이 다른 처음 85일 제거, forward-fill을 했다. resp~resp_4 전체를 multi-label로 학습하고, 샘플 가중치는 resp 절댓값 평균(큰 움직임에 집중)으로 줬다. Gaussian noise, swish, seed 3개 평균을 쓰고, 마지막 2개 fold 모델만 사용했다. AE-MLP 단일 모델도 private 1위 점수였다. — [Yirun's Solution (1st place)](https://www.kaggle.com/competitions/jane-street-market-prediction/discussion/224348)
- 3위는 피처 엔지니어링과 CV 없이 49층 MLP 15개를 앙상블했다. f0 조건부 median으로 NaN을 채웠고 loss는 BCE와 utility-like loss를 섞었다. seed 의존성이 커서 "randomness를 randomness로 상쇄"하려고 많은 seed를 평균했다. epoch 수는 public LB로 정했다. — [3rd place](https://www.kaggle.com/competitions/jane-street-market-prediction/discussion/224713)

**Jane Street Real-Time Market Data Forecasting (2024–25)**
- Private LB 8위(글 제목 기준). date_id 700 이후 데이터만 썼고, 시장 평균(date·time별), symbol별 최근 1000 time_id rolling mean/std, time_id를 피처로 넣었다. 하루를 sequence로 보는 GRU 2종에 seed 3개를 썼다. 보조 target으로 responder_7, 8과 이들로 만든 장기 평균 2개를 추가해 +0.001을 얻었다. online learning(하루 데이터가 오면 lr 3e-4로 한 번 update)은 CV +0.008로 가장 큰 개선이었다. MLP, time-series transformer, cross-symbol attention은 효과가 없었다. — [[Private LB 8th] solution](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/556542)
- 2위 Patrick Yam: 발표 영상과 slides가 있다(asset attention, temporal GRU, delayed online update, seed ensemble). 제3자가 독립적으로 재현한 결과에서는 frozen 17-model 앙상블 0.01413이 online learning(lr 1e-4)으로 0.01942가 됐다. 저자는 공식 재현이 아니라고 밝혔다. — [2nd Place Reproduction Attempt](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/742687), [cweill/jane-street-causal-forecasting](https://github.com/cweill/jane-street-causal-forecasting)
- 참가자 교훈 글: online learning이 약 +0.003이었다(당시 최고 public 0.009). lag를 입력 피처로 쓰는 것은 효과가 약했다. 학습 시에도 제출 API와 같은 입력과 update 빈도를 써야 한다. 그러지 않으면 학습 지표만 높고 hidden test에서는 성능이 없었다. — [Few lessons learned](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/547060)
- 공개 26위 팀: TabM(3층 MLP 512), time_id를 category로 넣으니 크게 개선됐다. loss로 R² loss가 MSE보다 epoch 간에 더 안정적이었다. GBDT offline 모델, ridge와 앙상블했다. — [[Public LB 26th]](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/556610)

**G-Research Crypto Forecasting (2022)**
- 데이터 페이지가 공개 LB의 target이 학습 데이터에 들어 있다고 명시했다. 참가자가 train.csv만으로 LB 0.99를 냈다. 즉 public LB는 의미가 없었다. — [Data](https://www.kaggle.com/competitions/g-research-crypto-forecasting/data), [Watch out! test LB period is contained in the train csv](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/285505)
- 2위 Nathaniel Maddux. 6-fold walk-forward grouped CV(group=timestamp)를 썼다. 각 fold는 학습 40주, 검증 40주, 1주 gap이고 20주씩 밀어서 겹치게 했다. 모델은 기본 LightGBM regressor(squared loss) 하나이고 앙상블, regularization, feature neutralization은 CV에서 도움이 안 돼서 뺐다. 피처는 Numba로 만들었고 전체 데이터로 학습했다. 제출은 두 번뿐이었다. 1위 팀명은 "Meme Lord Capital"이라고 적었다. — [2nd place solution](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/323098)
- 3위 sugghi. Close만 썼고 코인별 모델에 공통 피처를 줬다. 각 코인 변화와 전체 코인 평균 변화의 차이(시장 상대 피처)가 잘 먹혔다. 모델은 LightGBM 하나에 EmbargoCV 7-fold다. — [3rd place solution](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/323703)

**Ubiquant Market Prediction (2022)**
- 1위 yuuniee. LightGBM 5-fold와 TabNet 5-fold를 평균했다. 피처는 기본 300개에, target과 상관이 높은 상위 100개 피처의 time_id 평균 100개를 더했다(CV 0.141→0.154). RAM 때문에 최근 240만 행만 썼다. 피처·튜닝은 PurgedGroupTimeSeries와 TimeSeriesSplit으로 판단하고, 최종 학습은 KFold로 했다. 본인도 운이 좋았다고 적었다. — [1st Place Solution](https://www.kaggle.com/competitions/ubiquant-market-prediction/discussion/338220)
- 2위는 time_id 평균 100개와 행 단위 통계 5개, purged K-fold + embargo, LightGBM 5개다. 효과가 없었던 것: AE-MLP, feature neutralization, PCA. — [2nd Place – Robust CV and LGBM](https://www.kaggle.com/competitions/ubiquant-market-prediction/discussion/338615)
- 3위 hyd는 종목 3,500개를 sequence로 넣은 6층 transformer에 Pearson loss를 바로 최적화했다. time_id 평균 피처, clipping, target normalization은 효과가 없었다. — [3rd Place](https://www.kaggle.com/competitions/ubiquant-market-prediction/discussion/338561)

**JPX Tokyo Stock Exchange Prediction (2022)**
- 1위는 kisa031(Shoki Sakai, Shizuoka University)이다. — [Shizuoka Univ. 뉴스](https://www.inf.shizuoka.ac.jp/english/news/detail.html?CN=154751)
- 대회 뒤 한 참가자가 1위와 2위 코드를 분석했다. 두 해법 모두 피처를 종목별이 아니라 전체 데이터에 걸쳐 계산했고 데이터 순서에 민감했다. 2위 피처를 의도대로 고치면 점수가 -0.214였다. "상위 2팀은 순전히 운"이라는 결론이다. — [About 1st place solution](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/363838), [About the 2nd place solution](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/364227), [Errors, Luck, and Winners](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/364262)
- 4위는 ML 없이 1일 수익률 역순 순위(단기 reversal)를 쓰고 ExpectedDividend>0 종목을 최하위로 뒀다. 랜덤 모델 점수가 대략 N(0, 0.138)이라 −0.3~0.3 점수로는 좋은 모델인지 판단할 수 없다고 봤다. 제출 2개를 서로 뒤집은 순위로 내서 점수가 양수가 되도록 했다. — [4th place solution](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/359151)
- 10위: 단순한 수익률·변동성·이동평균 피처와 LightGBM, walk-forward CV. — [10th Place](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/361127)

**2025–2026 대회**
- DRW 1위 A_A. 주력은 3층 MLP(single private 0.131)이고 XGB가 보조다. 검증은 purged group time-series split(6그룹, 약 2개월씩)이었다. 피처는 다음 순서로 골랐다: 상관 0.6 이상 클러스터에서 medoid만 남김 → fold마다 SHAP top-20에 반복해서 들어온 것만 남김 → 선형 결합 피처를 추가 → AutoEncoder로 deep feature 8개를 만듦. 학습은 SGD가 Adam보다 나았고 loss는 0.6·MSE + 0.4·Pearson이었다. 시간이 지나 예측력이 사라진 피처가 있어서 버렸던 피처를 다시 하나씩 넣어 보기도 했다. — [DRW solution 1st](https://www.kaggle.com/competitions/drw-crypto-market-prediction/discussion/599068)
- DRW 2위는 시간대 클러스터 단위로 행을 지웠다 넣었다 하면서 public LB 점수로 학습 데이터를 골랐다. 사실상 LB probing이라 일반화 근거가 약하다. — [2nd & 25th Place](https://www.kaggle.com/competitions/drw-crypto-market-prediction/discussion/596581)
- Hull Tactical 4위는 학습 모델 없이 대회 전에 미리 검증한 short-term reversal alpha 하나를 썼다. 핵심은 portfolio 구성이었다: inverse-volatility 가중과 volatility targeting. "예측을 더 잘하는 것보다 portfolio 구성이 더 중요했다"고 적었다. — [[4th Place] Short Term Reversal](https://www.kaggle.com/competitions/hull-tactical-market-prediction/discussion/718664)
- Hull 상위 1~3위 write-up은 2026-07-02 기준 보이지 않는다는 참가자 글이 있다. — [Where are the top solutions?](https://www.kaggle.com/competitions/hull-tactical-market-prediction/discussion/717746)
- Mitsui 5위(private 0.532). 4일 window RNN과 단일 일자 MLP를 평균했다. 피처 가공 없이 raw를 그대로 쓰고 결측은 -1로 채웠다. loss는 MSE와 ranking loss를 합쳤다. window를 길게 하면 오히려 잡음이 늘었다. — [ZLF's Solution](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/670526)
- 2025년에 가장 참가자가 많았던 대회로 Jane Street RTMDF(3,700팀 이상)가 꼽혔다. — [ML Contests: State of ML Competitions 2025](https://mlcontests.com/state-of-machine-learning-competitions-2025/)

**Numerai**
- target은 market/country/sector/common factor 노출을 뺀 residual return이다. target 기간이 서로 겹치므로 CV에 주의하라고 문서가 경고한다. 일부 피처에 크게 의존하지 말라는 권고도 있다(예측력이 시간에 따라 변함). — [Numerai data docs](https://docs.numer.ai/numerai-tournament/data)
- 점수는 CORR(target과의 상관)과 MMC(Meta Model 기여도)다. 20일 round는 4주, 60일 round는 12주 뒤에 결과가 확정된다. — [Numerai docs](https://docs.numer.ai/)
- 공식 예제 저장소 [numerai/example-scripts](https://github.com/numerai/example-scripts)(MIT, 2026-10-16 push)와 [numerapi](https://pypi.org/project/numerapi/)(3.1.1, 2026-08-12)는 계속 관리되고 있다.

### Inferences
- **피처**: 거의 모든 상위 해법이 같은 시점의 다른 종목이나 시장 전체와 비교한 상대값(평균·median과의 차이, rank, 시장 평균 피처)을 핵심으로 썼다(OTC 1·7위, GR 3위, Ubiquant 1·2위, JSRT 8위). order book imbalance는 기본으로 깔고 시작한다.
- **모델**: 데이터가 작거나 중간 크기이면 LightGBM 하나로도 1위가 가능했다(ORVP, GR 2위). NN이 이긴 대회는 데이터가 크고 online update가 가능한 곳(OTC, JSRT)이었다. 앙상블 효과의 상당 부분은 seed 평균으로 얻었다.
- **검증**: purged/embargo, gap이 있는 walk-forward를 쓰고 fold를 겹쳐 길게 잡았다. CV 분산을 seed를 바꿔 확인했다. public LB는 믿지 않았다(GR, Hull, Mitsui는 대회 규정상 public LB가 무의미).
- **Leakage 교훈**: 데이터 정규화를 되돌리는 leak(ORVP tick size), CV 밖에서 학습한 autoencoder(JSMP), 종목을 섞어 계산한 rolling 피처(JPX 1·2위), public LB 기간이 학습 데이터에 포함된 것(GR)이 있었다.
- **효과가 없었던 것(여러 번 반복 보고됨)**: feature neutralization, PCA, rank normalization, 더 큰 transformer, 1D-CNN/MLP를 추가한 앙상블, 대부분의 target clipping과 normalization.

### Gaps
- Jane Street RTMDF 1위와 G-Research 1위(Meme Lord Capital) write-up은 Kaggle 포럼에서 찾지 못했다. GR은 "winning solution 정보가 있느냐"는 질문 글만 있다([325776](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/325776)).
- JPX 1위의 원래 해법 설명, Hull 1~3위, Mitsui 1~4위 write-up은 확인하지 못했다.
- JSRT "Private LB 8th"는 글 제목 기준이다. 최종 리더보드 순위는 직접 확인하지 못했다.
- web search 한도 때문에 Kaggle 밖 2025–2026 대회(ADIA Lab Market Prediction(CrunchDAO), ACM ICAIF 2025 crypto 대회 등)는 상세를 확인하지 못했다.

## 2. Factor libraries: WorldQuant 101, Qlib Alpha158/Alpha360, GTJA 191

### Takeaway
세 라이브러리 모두 일봉 OHLCV/VWAP으로 만든 가격·거래량 formula 모음이다. 1분봉에 window만 바꿔 옮겨 LightGBM 후보 피처 목록으로 쓸 수는 있다. 하지만 그대로는 4분 horizon에 맞지 않는다.

### Cited Findings
- **101 Formulaic Alphas**(Kakushadze, arXiv 1601.00991, Wilmott 2016). 실제 운용된 alpha 101개를 수식으로 공개했다. 평균 보유기간은 0.6~6.4일이고 alpha 간 평균 상관은 15.9%다. 수익은 변동성과 강하게 상관되고 turnover와는 유의한 관계가 없다. — [arXiv](https://arxiv.org/abs/1601.00991)
- **Qlib Alpha158**: KBAR 피처, 당일 가격 비율, rolling 연산자(ROC, MA, STD, BETA(slope), RSQR, RESI, MAX, MIN, QTLU/QTLD, RANK 등)를 window 5/10/20/30/60에 적용해 만든다. **Alpha360**은 최근 60일 가격과 거래량을 정규화해 그대로 넣는다. — [qlib/contrib/data/loader.py](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)
- Qlib 기본 label은 `Ref($close,-2)/Ref($close,-1)-1`(T+1→T+2)이다. 이유는 중국 주식이 T+1 매매이기 때문이다. 1분 데이터(`--interval 1min`)와 high-frequency 예제도 있다. — [Qlib data docs](https://qlib.readthedocs.io/en/latest/component/data.html), [examples/highfreq](https://github.com/microsoft/qlib/tree/main/examples/highfreq)(Alpha158 tree 설정 포함)
- **GTJA 191**: DolphinDB 공식 모듈이 국태군안증권(Guotai Junan)의 191개 alpha formula를 구현했고 "일봉 데이터 기준"이라고 적혀 있다. 같은 저장소에 WorldQuant 101 모듈(`wq101alpha`)도 있다(Apache-2.0). — [DolphinDBModules/gtja191Alpha](https://github.com/dolphindb/DolphinDBModules/tree/master/gtja191Alpha)

### Inferences
- 단기용으로 옮길 만한 것은 KBAR(몸통·꼬리 비율), 짧은 window의 ROC/STD/RSQR/RESI, 거래량-가격 상관 정도다. 대부분 krx-quant가 이미 가진 피처와 겹칠 가능성이 높다.
- 101/191 수식에는 종목 간 cross-sectional `rank()`가 많다. 종목이 3개뿐이면 그 부분의 의미가 거의 없어진다.

### Gaps
- GTJA 191 원 보고서(국태군안 단기 가격·거래량 다요인 보고서)의 원문 URL과 발행일은 확인하지 못했다.
- Alpha158의 정확한 158개 구성은 loader 소스로만 확인했다. 개수를 세는 검증은 하지 않았다.

## 3. 오픈소스 프레임워크·라이브러리 (2026-10-17 기준)

### Takeaway
signals-only 엔진에는 체결을 시뮬레이션하는 backtester(hftbacktest, NautilusTrader, LEAN)가 필요 없다. 쓸모 있는 것은 지표 계산(TA-Lib), 라벨·CV 유틸리티(mlfinpy 또는 직접 구현), factor IC 분석 아이디어(alphalens) 정도다. backtrader는 사실상 관리가 멈췄고, mlfinlab과 vectorbt PRO는 상용이다.

### Cited Findings (표)

| 도구 | 잘하는 것 | Licence | 2026 상태 | Tick/LOB | LightGBM 엔진 적합도 |
|---|---|---|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | factor 데이터셋, 모델 zoo, 워크플로 | MIT | v0.9.8 (2026-10-08), 활발 | 1min, highfreq 예제 | 중: 수식만 참고, 프레임워크는 무거움 |
| [nkaz001/hftbacktest](https://github.com/nkaz001/hftbacktest) | queue position과 latency를 반영한 L2/L3 tick 재생 backtest(Numba/Rust) | MIT | rust-v0.9.4 / py 2.4.4 (2025-12-10), 마지막 commit 2025-12-23 | L2 MBP, L3 MBO | 낮음: 체결 시뮬레이션용 |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | 대규모 파라미터 grid를 벡터화한 backtest | Apache-2.0 + Commons Clause("Fair Code"). PRO는 상용 | v1.1.2 (2026-10-09), 활발 | bar | 낮음~중 |
| [mementum/backtrader](https://github.com/mementum/backtrader) | event-driven bar backtest | GPL-3.0 | 마지막 commit 2023-04 | bar | 비추천 |
| [stefan-jansen/zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | Quantopian식 bar backtest | Apache-2.0 | 3.1.1 (2025-07), 마지막 commit 2025-11 | bar | 낮음 |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | Rust 코어 event-driven 엔진, backtest와 live 코드 공유 | LGPL-3.0 | v1.231.0 (2026-08), commit 매일 | quote/trade tick, order book, ns 해상도 | 낮음: 실행 엔진 |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | C# 엔진에 Python 알고리즘 지원, 클라우드 연동 | Apache-2.0 | 2026-10-16 push, `lean` CLI 1.0.230 | [미확인: tick 지원 문서 직접 확인 안 함] | 낮음 |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 심층 강화학습(DRL) 트레이딩 | MIT | GitHub v0.3.8 (2026-03), PyPI 0.3.7 (2024-04) | 일봉 예제 중심 | 범위 밖 |
| [hudson-and-thames/mlfinlab](https://github.com/hudson-and-thames/mlfinlab) | López de Prado 기법(bars, triple barrier, purged CV) | 상용(공개 repo는 이슈 접수 전용) | 공개 repo 2023-10 | — | 비추천(유료) |
| [baobach/mlfinpy](https://github.com/baobach/mlfinpy) | mlfinlab을 오픈소스로 다시 구현 | MIT | 0.1.2 (2024-10), commit 2026-10, star 89 | — | 중: 일부 코드만 참고 |
| [stefan-jansen/alphalens-reloaded](https://github.com/stefan-jansen/alphalens-reloaded) | factor IC, quantile return 분석 | Apache-2.0 | 0.4.6 (2025-06), commit 2026-10 | 일 단위 | 중: 개념 참고 |
| [TA-Lib/ta-lib-python](https://github.com/TA-Lib/ta-lib-python) | C로 만든 기술지표 150여 개 | BSD-2 | 0.8.2 (2026-10-12), 활발 | 배열 입력 | 높음 |
| pandas-ta ([PyPI](https://pypi.org/project/pandas-ta/)) | pandas 기반 지표 | PyPI licence 필드 비어 있음 | PyPI 0.4.71b0 (2025-09, Py≥3.12). GitHub `twopirllc/pandas-ta`는 API에서 404 | — | 낮음: 상태 불명확 |
| [xgboosted/pandas-ta-classic](https://github.com/xgboosted/pandas-ta-classic) | pandas-ta fork | MIT | 0.8.32 (2026-09) | — | 중 |
| [sktime](https://github.com/sktime/sktime) / [darts](https://github.com/unit8co/darts) | 범용 forecasting API | BSD-3 / Apache-2.0 | 1.2.0 (2026-09) / 0.41.0 (2026-02) | — | 낮음 |
| [Nixtla mlforecast](https://github.com/Nixtla/mlforecast) · [statsforecast](https://github.com/Nixtla/statsforecast) · [neuralforecast](https://github.com/Nixtla/neuralforecast) | lag 피처와 LightGBM 시계열 예측 / 통계 모델 / NN | Apache-2.0 | 1.1.0 / 2.1.1 / 3.3.0 (2026), 활발 | — | 중: 엔진이 이미 비슷한 것을 갖고 있음 |

출처: GitHub API(stars, licence, 마지막 commit, release)와 PyPI JSON, 2026-10-17 조회. 기능 설명은 각 README에서 가져왔다. hftbacktest의 "queue position, latency, L2/L3" 설명은 [README](https://github.com/nkaz001/hftbacktest)에서 왔다. Nautilus의 "quote/trade tick, bar, order book, nanosecond"는 [README](https://github.com/nautechsystems/nautilus_trader)에 있다. vectorbt의 "Fair Code"와 PRO 기능 차이는 [README](https://github.com/polakowo/vectorbt)와 [LICENSE](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md)에 있다. mlfinlab이 closed-source라는 내용은 [mlfinpy README](https://github.com/baobach/mlfinpy)와 [mlfinlab README](https://github.com/hudson-and-thames/mlfinlab)에서 확인했다.

### Inferences
- hftbacktest는 2025-12 이후 commit이 없다. 유지보수가 느려졌다고 볼 수 있지만, 폐기 선언은 확인하지 못했다.
- vectorbt의 Commons Clause는 "판매"만 막는다. 개인 연구에는 문제가 없어 보인다(법률 판단 아님).

### Gaps
- pandas-ta GitHub 저장소가 404가 된 이유(삭제, 비공개 전환, 이전)는 확인하지 못했다.
- LEAN과 zipline-reloaded의 tick·분봉 지원 범위는 문서를 직접 확인하지 못했다.

## 4. 한국 시장 오픈소스와 커뮤니티

### Takeaway
KIS 공식 샘플 저장소가 활발하게 관리되고 있고, 최근 LLM용 예제와 LEAN 기반 backtester까지 추가됐다. pykrx와 FinanceDataReader는 일봉이나 참고 데이터용 scraping 도구다. 한국 퀀트 커뮤니티는 이번에 출처를 확보하지 못했다.

### Cited Findings
- [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api): KIS Open API 공식 샘플이다. LLM 탐색용 `examples_llm/`, 상품별 통합 예제 `examples_user/`, `strategy_builder/`, QuantConnect Lean 기반 `backtester/`, `llms.txt`가 있다. 2026-10-14 commit, star 1,649이고 licence 파일은 감지되지 않았다. — [README](https://github.com/koreainvestment/open-trading-api)
- [sharebook-kr/pykrx](https://github.com/sharebook-kr/pykrx): KRX와 Naver에서 주식·채권 데이터를 scraping한다. 데이터 저작권은 제공처에 있고 공식 데이터와 다를 수 있다고 README에 적혀 있다. PyPI 1.2.9(2026-09-19, MIT 표기). GitHub licence 파일은 감지되지 않았다. — [README](https://github.com/sharebook-kr/pykrx), [PyPI](https://pypi.org/project/pykrx/)
- [FinanceData/FinanceDataReader](https://github.com/FinanceData/FinanceDataReader): KRX 상장·폐지 종목, 국내외 가격, 지수, 환율, 암호화폐 데이터를 읽는다. MIT, 0.9.202(2026-05-13). — [README](https://github.com/FinanceData/FinanceDataReader)
- [quantylab/rltrader](https://github.com/quantylab/rltrader): MIT, star 366, 2026-07 push. 내용(README)은 읽지 않았다.

### Gaps
- 한국 퀀트 커뮤니티(네이버 카페, 블로그, DACON 금융 대회 등)는 web search 한도 때문에 검증 가능한 URL을 확보하지 못했다.

## krx-quant 적용 메모

엔진 맥락: 3종목, 4분 horizon, nightly retrain, 2 GB 서버. 검증 도구로 `krx-quant replay`와 `prevalidate`가 있고, walk-forward 기본 구간은 20거래일(Turing 60)이다.

**해볼 만한 피처·모델 기법**
1. **시장 대비 상대 피처**: 종목이 3개라 종목 간 rank는 약하다. 대신 KOSPI200 선물이나 종목 2개의 평균을 "시장"으로 두고 수익률·OFI·잔량 불균형에서 시장 부분을 뺀 deviation 피처를 만든다(OTC 7위, GR 3위, Ubiquant 1·2위). 참고 target으로 β×선물 수익률을 뺀 residual 방향도 비교해 볼 만하다(GR, Numerai의 residualized target).
2. **Nearest-neighbour regime 피처**(ORVP 1위): 과거 같은 시간대 중 변동성·거래량·OFI가 가장 비슷한 k개 구간의 이후 4분 realized range 평균. HAR range(Mandelbrot)와 barrier 폭 산정에 넣는다. 미래 구간은 쓰지 않는다.
3. **Target 변환**(ORVP 3위): range와 변동성 모델은 절대값 대신 "다음 구간 RV ÷ 직전 구간 RV"를 예측해 비정상성을 줄인다.
4. **시간대 구간 피처**(OTC 1위의 `seconds_in_bucket` group): 장 시작, 장중, 마감 동시호가 직전처럼 시간대를 나누고 구간 첫 값 대비 비율과 구간 내 expanding mean을 피처로 쓴다.
5. **Multi-horizon 보조 target**(JSMP 1위, JSRT 8위): 1·4·10분 라벨을 함께 학습하거나 확률을 평균한다. LightGBM이라면 horizon별 모델을 따로 두고 그 확률을 ensemble 입력으로 쓴다.
6. **|수익률| sample weight**(JSMP 1위): up/down 적중에 집중하려면 큰 움직임에 가중치를 준다. flat 클래스가 과하게 학습되는 것을 막는 효과도 기대된다(검증 필요).
7. **Seed 평균과 안정 피처 선택**(JSMP 3위, DRW 1위): LightGBM `bagging_seed`/`feature_fraction_seed`를 3~5개로 바꿔 평균한다. fold마다 SHAP top-k에 반복해서 들어오는 피처만 남긴다.
8. **더 잦은 update**(OTC 1위 5회 재학습, JSRT online learning +0.003~0.008): nightly 재학습은 이미 이 방향이다. 장중 incremental update(LightGBM `init_model`)는 이득에 비해 메모리 위험이 커서 보류하는 것이 맞아 보인다. 2 GB 서버라면 OTC 1위처럼 하루 단위 parquet 파일을 float32 배열로 조금씩 읽어 오는 방식이 참고가 된다.

**검증과 leakage 체크**
- purged walk-forward에 **embargo ≥ label horizon(4분) + 가장 긴 피처 window**를 둔다(JSMP 31-gap, GR 1주 gap, Ubiquant purged K-fold + embargo). fold를 겹쳐 길게 잡고 seed를 바꿔 CV 분산을 확인한다(GR 2위).
- 모든 rolling 피처는 **종목별로** 계산하고, 데이터 순서를 섞어도 값이 같은지 테스트한다(JPX 1·2위의 실패).
- 적중률 차이의 유의성: JPX에서는 랜덤 점수 σ≈0.138 때문에 상위권이 운으로 갈렸다. 10월 초부터 쌓인 체결·호가 데이터는 아직 짧으므로, 모델 비교는 majority-class 기준선 대비 binomial test나 bootstrap 신뢰구간으로 한다.
- 효과가 없었다는 보고가 많은 것은 먼저 시도하지 않는다: feature neutralization, PCA, rank normalization, 큰 NN.

**도구 채택 판단**
- **채택하지 않음, 자체 replay·scoring 유지**: hftbacktest, NautilusTrader, LEAN, vectorbt, backtrader, zipline. 이것들은 주문 체결 시뮬레이션용이다. signals-only 엔진의 up/flat/down 적중률 평가는 지금의 `replay`/`prevalidate`가 더 잘 맞는다.
- **부분 활용**: TA-Lib(BSD, 활발)은 지표 계산용. mlfinpy(MIT)의 purged K-fold와 triple-barrier 코드는 비교·참고용으로만 보고 의존성으로 넣지 않는다. Qlib Alpha158, WorldQuant 101, GTJA 191은 1분봉용 피처 아이디어 목록으로만 쓴다.
- **피함**: mlfinlab(상용), pandas-ta 원 저장소(상태 불명확, 필요하면 pandas-ta-classic), FinRL(DRL, 범위 밖).
- KIS `open-trading-api`의 `backtester/`(Lean 기반)는 참고만 한다. 엔진 데이터 경로와는 별개다.
