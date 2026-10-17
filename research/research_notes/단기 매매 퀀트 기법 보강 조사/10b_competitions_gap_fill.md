# 시장 예측 대회·오픈소스 보강 조사 (10번 노트 Gaps 메우기)

> 조사일 2026-10-17. 연구 노트이며 투자 조언이 아니다.
> 앞 노트: `단기 매매 퀀트 기법 조사/10_competitions_opensource.md`. 여기에는 그 노트에 없는 것만 적는다.
> 방법: Kaggle 최종 순위, 팀원, write-up 연결 여부, 토론글 원문은 Kaggle 사이트가 화면에 쓰는 JSON 응답(사이트에 보이는 것과 같은 데이터)을 직접 읽었다. 링크는 사람이 열 수 있는 Kaggle 페이지 주소로 적었다. 저장소 수치는 GitHub API, 패키지 정보는 PyPI JSON에서 같은 날 가져왔다. 이번 조사에서 WebSearch는 12번 썼고 한도에 걸리지 않았다.
> "write-up 없음"은 Kaggle 리더보드의 팀별 write-up 연결 칸이 비어 있고, 대회 포럼 전체 글 목록에서 그 팀원이 쓴 해법 글을 찾지 못했다는 뜻이다.

## 0. 앞 노트 Gaps 대조표

| 앞 노트의 Gap / 미확인 | 결과 | 요약 |
|---|---|---|
| Jane Street RTMDF 1위 write-up | **여전히 없음** | 1위 "ms capital"(GM 3명). write-up 없음. 2위 Patrick Yam은 Kaggle 공식 영상과 slides가 있음. 9위 CatBoost online 요령 글을 새로 찾음 |
| JSRT "Private LB 8th"의 실제 순위 | **확인** | 최종 private 8위(0.010434), Evgeniia Grigoreva |
| G-Research 1위(Meme Lord Capital) write-up | **write-up은 없음, 요약만 찾음** | G-Research 공식 블로그: 상위 3팀 모두 LightGBM, 승인은 feature engineering. 7·9·13위 글을 새로 확인 |
| JPX 1위 원 해법 설명 | **여전히 없음** | 1위 kisa031(s_sakai)의 write-up 없음. 제3자가 "1위 추정" notebook을 linear regression으로 분류. 앞 노트의 Shizuoka Univ. 링크는 지금 404 |
| Hull 1~3위 write-up | **여전히 없음** | 1위 lunalu, 2위 FastScramblers, 3위 wynzaro. 2026-10-17 현재 write-up 연결 없음 |
| Mitsui 1~4위 write-up | **3위만 찾음** | 3위 Ayush Khaire 글 확보. 1·2·4위는 없음. 6·7·8·10·15위 글과 주최 측 발표 자료를 추가로 확보 |
| Kaggle 밖 대회(ADIA Lab/CrunchDAO 등) | **일부** | ADIA Lab Market Prediction의 과제와 지표 확인. 우승 해법은 못 찾음. Numerai Signals 개요 확인 |
| 앞 노트가 놓친 2023–2026 대회 | **새로 찾음** | Kaggle "MSCapital – Real Financial Market Forecasting"(2026-07~10, 중국 A주 3초 snapshot, 1/5/30분 수익률). DACON 제2회 KRX 주식 투자 알고리즘 경진대회(2023) |
| GTJA 191 원 보고서 | **확인** | 국태군안증권(Guotai Junan), 2017-06-15, 「基于短周期价量特征的多因子选股体系——数量化专题之九十三」. 과제 설명의 "2014"는 틀렸다 |
| pandas-ta GitHub 404 이유 | **이유는 미확인, 현상은 확인** | 저장소 404, 소유 계정 공개 repo 0개, PyPI 옛 버전 삭제, pandas-ta.dev 접속 안 됨. 대체: pandas-ta-classic, ft-pandas-ta, TA-Lib |
| Alpha158 개수 검증 | **확인** | KBAR 9 + 가격 4 + rolling 연산자 29×window 5 = 158 |
| LEAN, zipline-reloaded의 tick/분봉 지원 | **확인** | LEAN `Resolution`: Tick/Second/Minute/Hour/Daily. zipline-reloaded `--data-frequency`: daily/minute |
| quantylab/rltrader README | **확인** | 「파이썬을 이용한 딥러닝/강화학습 주식투자」(개정2판) 예제 코드 |
| 한국 퀀트 커뮤니티 | **일부** | DACON 금융 대회, KIS·키움 REST API, OpenDART, 책 예제 저장소는 확인. 네이버 카페와 개인 블로그는 신뢰할 만한 출처를 못 찾음 |

## 1. 빠졌던 상위 해법

### Takeaway
5개 대회의 최종 1위 중 Kaggle에 해법을 올린 팀은 하나도 없다(JSRT, GR, JPX, Hull, Mitsui 모두). 대신 2·3위권과 공식 영상에서 새 근거를 얻었다. JSRT 2위의 핵심은 최근 날짜에 더 큰 sample weight, 많은 seed, 최신 하루만으로 하는 짧은 online update였다. GR 상위권은 모두 LightGBM과 feature engineering으로 이겼다. Mitsui 3위는 검증을 거의 하지 않았다고 스스로 밝혀서 운의 비중이 크다.

### Cited Findings

**Jane Street Real-Time Market Data Forecasting (최종 private, 2025-07-12 마감)**
- 최종 1~10위와 점수: 1 ms capital 0.013890(chenxin1991, wht1996, dc5e964768ef56302a32, 셋 다 Grandmaster) / 2 Patrick Yam 0.013273 / 3 yuanzhe zhou 0.013163 / 4 Haoze Hou 0.011683 / 5 hyd 0.011449 / 6 Thomas Dueholm Hansen 0.010675 / 7 leo 0.010480 / 8 Evgeniia Grigoreva 0.010434 / 9 HAO LI 0.010417 / 10 ponythewhite 0.010293. 1~7위는 write-up 연결이 비어 있고, 8위만 연결돼 있다. — [Leaderboard](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/leaderboard)
- 주최 측 정리: 등록 25,703명, 참가 3,643명, 2,781팀(규정 위반 팀 제거 뒤), 102개국, 제출 4,294건. 앞 노트 표의 3,757은 대회 메타데이터 값이다. — [Recap](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/593758)
- 2위 Patrick Yam의 Kaggle 공식 영상: "Kaggle Winners Walkthroughs: Jane Street Real-Time Market Data Forecasting with Team Patrick Yam". — [YouTube](https://www.youtube.com/watch?v=lfzzPZZyzjE), [slides(Google Drive)](https://drive.google.com/file/d/1AdzXrOZN699SGHAyb32slLBkEMxBCYiJ/view)
- 아래는 영상과 slides를 제3자가 옮겨 적은 내용이다(저자 공식 코드가 아님). 모델은 asset attention, 1층 temporal GRU, gated FFN으로 된 block 8개다(폭 64, RMSNorm, SiLU, AdamW lr 5e-4). sample weight는 날짜 d에 `(200+d)/(200+d_max)`를 곱해 최근 날짜를 무겁게 했다. epoch 수는 seed마다 따로 고르지 않고 하나로 고정했다(seed별 best epoch 선택이 noisy했기 때문). seed는 늘릴수록 좋아졌지만 증가폭은 줄었다(17 model). online update는 새로 label이 나온 날마다 그 하루로만 Adam 3 step(betas 0.8, 0.95)을 돌렸다. offline optimizer를 이어 쓰거나 여러 지난 날을 다시 학습하면 결과가 나빠졌다고 한다. 저자가 보고한 local 점수 변화: 첫 모델 0.01320 → tuning 0.01695 → sample weighting 0.01811 → seed 추가 0.01890 → post-norm 제거 0.01967 → GRU 폭 ×4 0.02059. — [cweill/jane-street-causal-forecasting tracker](https://github.com/cweill/jane-street-causal-forecasting/blob/HEAD/docs/patrick-yam-tracker.md)
- 같은 재구성에서 17-model 앙상블은 online(lr 1e-4) 0.02078, frozen 0.01433이었다(date 1500–1698). 저자는 리더보드와 같은 결과라는 보장이 없다고 밝혔다. — [README](https://github.com/cweill/jane-street-causal-forecasting)
- 9위 HAO LI(대회 중 공개): CatBoost로 online training을 시간 제한 안에 하는 요령. (1) 1,500 iteration을 한 번에 돌리지 않고 300씩 5번 나눠 학습하면서, 앞 단계 예측을 다음 단계의 `baseline`으로 넣었다. (2) date_id마다 행의 40~50%만 표본으로 뽑고 최근 600 date_id만 써도 검증 점수가 거의 같았다(100%: 0.01754, 50%: 0.0177). 180 feature, 7일마다 재학습, anomaly 제거와 uncertainty 기반 post-process로 당시 LB 0.008 이상을 냈다. — [Tricks to make CatBoost online training great again](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/556544)
- 같은 참가자는 lag responder를 주는 새 API 설계 덕분에 첫 private 갱신이 "의외로 안정적"이었고, 금융 대회의 큰 shake-up 시대가 끝났다고 봤다(의견). — [discussion/562410](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/discussion/562410)

**G-Research Crypto Forecasting (2022-05 마감)**
- 최종: 1 Meme Lord Capital 0.0326(Jose Antonio Alatorre, Eduardo Peynetti) / 2 Nathaniel Maddux 0.0283 / 3 GABA(sugghi) 0.0232 / 7 Patrick Yam 0.0202 / 9 bturan19 0.0194. 1위는 write-up 연결이 없다. — [Leaderboard](https://www.kaggle.com/competitions/g-research-crypto-forecasting/leaderboard)
- G-Research 공식 정리(2022-05-30): 1위는 Meme Lord Capital의 Jose와 Eduardo이고 상금은 $50,000이다. "all of our top three used a LightGBM model". 상위 팀 모두 가장 큰 기여는 feature engineering이었다고 답했다. 점수는 초반에 크게 흔들렸다. — [G-Research news](https://www.gresearch.com/news/wrapping-up-the-g-research-crypto-forecasting-competition/)
- 7위 Patrick Yam: "99% modeling". 직접 추가한 feature는 time of day 하나다. sequence 길이가 다른 모델 4개를 앙상블했다. 구조는 이미지로만 공개됐다. — [7th place](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/323250), 코드 [wimwimyam/kaggle-g-research-crypto-forecasting](https://github.com/wimwimyam/kaggle-g-research-crypto-forecasting)
- 9위 bturan19: 가장 좋았던 feature는 Hull moving average(`close − HMA(240)`)다. window는 Fibonacci 수열(55, 210, 340, 890, 3750)로 잡았다. 공식 target 대신 `close/close[-16]−1`을 lag target으로 쓰고, 그 값의 자산 평균을 시장 지표로 넣었다. 상승장·하락장·횡보장용 LightGBM 3개를 따로 학습해 평균했다. 파라미터는 거의 기본값이었다. — [9th place](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/324180)
- 13위 tomforbes(대회 6주차에는 1위): feature 17개(EMA, 과거 수익률, 과거 변동성의 lag 8개 + 같은 timestamp의 자산 평균 8개 + Asset_ID). LightGBM용으로 연속 feature를 500~1000개 값으로 binning했다. target은 "15분 forward return"과 "beta(시장) 성분"으로 나눠 재구성하고, 모델 2개를 따로 학습해 API 상황에 따라 바꿔 썼다(약 +0.01). LightGBM과 Keras NN 앙상블은 약 +0.005였다. CV는 expanding fold 5개와 시장 국면이 다른 test fold 2개로 했다. 효과가 없었던 것: 다른 거래소 데이터를 겹쳐 쓰기, **classification으로 바꾸기(성능 나쁨)**, LightGBM tuning, PCA, 14개 자산을 한 행에 넣기, GB와 NN 위에 meta model 쌓기. — [13th Place (Final) 1st Place (6 Weeks In)](https://www.kaggle.com/competitions/g-research-crypto-forecasting/discussion/313386)

**JPX Tokyo Stock Exchange Prediction (2022-10 마감)**
- 최종: 1 kisa031 0.381(멤버 표시명 s_sakai, 아이디 shokisakai) / 2 aa 0.356 / 3 xiaobenla 0.352 / 4 ddm 0.347 / 5 JonnydosSantos 0.339 / 6 Kento 0.308 / 7 BigStonks 0.301 / 8 Doctor 0.289. 1~3위는 write-up 연결이 없다. 주의할 점이 있다. 지금 리더보드에서 BigStonks는 7위, Doctor는 8위인데, 두 팀의 글 제목은 각각 "8th", "10th"다(대회 직후 순위에서 바뀐 것으로 보이나 이유는 미확인). 앞 노트의 "10위"도 이 제목을 따른 것이다. — [Leaderboard](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/leaderboard)
- 한 참가자가 정리한 공개 해법 목록: 1위는 "potentially the 1st place solution" notebook `shokisakai/jpx-regression`(Linear regression), 2위 `uioiuioi/2nd-place-solution`(LightGBM), 4위 1일 수익률 momentum, 5위 LightGBM, 8위와 10위 LightGBM. — [List of published top solutions](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/358938), [jpx-regression notebook](https://www.kaggle.com/code/shokisakai/jpx-regression)
- 2위 해법 글(361482)은 지금 "[Deleted Topic]"이다. 댓글만 남아 있다. — [discussion/361482](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/361482)
- 1위 추정 notebook을 복사해 늦게 제출하면 2위보다 높게 나오지만 1위 점수와 똑같지는 않다. 데이터를 Close로 정렬하는 줄 하나만 넣어도 점수가 −0.085가 됐다. 학습 때는 전체 데이터로 z-score를 계산하는데, 제출 때는 하루치만 들어오므로 z-score의 의미가 달라진다. — [About 1st place solution](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/363838)
- BigStonks(글 제목 8위, 현재 리더보드 7위): 33개 업종(33SectorName)별로 LightGBM regressor를 하나씩 학습하고, 예측한 변화율로 순위를 매겼다. Optuna로 전 종목 모델 하나를 tuning한 뒤 그 값을 업종 모델에 그대로 썼다. — [8th Place Solution](https://www.kaggle.com/competitions/jpx-tokyo-stock-exchange-prediction/discussion/359227)
- 앞 노트가 1위 신원 출처로 쓴 Shizuoka Univ. 뉴스 페이지는 2026-10-17 현재 HTTP 404다(영문·일문 경로 모두). 1위가 kisa031이라는 것은 위 리더보드로만 확인된다. — [이전 링크](https://www.inf.shizuoka.ac.jp/english/news/detail.html?CN=154751)

**Hull Tactical – Market Prediction (2026-06-25 마감)**
- 최종: 1 lunalu 3.845(samuel tan) / 2 FastScramblers 3.842 / 3 wynzaro 3.533 / 4 YannFb 3.347 / 5 wen chong yu 3.288. write-up 연결은 4위에만 있다(2026-10-17 기준). 포럼 전체 245개 글 중에도 1~3위 팀원의 글은 없다. — [Leaderboard](https://www.kaggle.com/competitions/hull-tactical-market-prediction/leaderboard)
- 주최 측 정리: 등록 17,926명, 참가 4,096명, 3,293팀, 104개국, 제출 5,612건. — [Recap](https://www.kaggle.com/competitions/hull-tactical-market-prediction/discussion/714630)

**MITSUI&CO. Commodity Prediction Challenge (2026-01-16 마감)**
- 최종: 1 anonemaus 0.63834 / 2 m1ngka1 0.61027(Wenxin Shi, Maaax, m1ngka1) / 3 Ayush Khaire 0.60229 / 4 Head of Meme Trading 0.54176 / 5 ZLF 0.53231 / 6 Poidenko Artem 0.51444 / 7 Tatru 0.50632 / 8 honkai01031227 0.49066 / 10 miguel perez 0.47974. — [Leaderboard](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/leaderboard)
- 1위 본인 글은 우승을 옛 상사에게 바친다는 짧은 글 하나다. 그 상사의 "old traders" 중 하나였다고 적었다. 해법 내용은 없다. — [Dedicating the win](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/687186)
- 3위 Ayush Khaire(Expert, 첫 시계열 대회). target 424개마다 모델을 하나씩 두고, `target_pairs.csv`에 적힌 그 target의 자산 1~2개 열만 feature로 썼다. 그 열에 lag, rolling mean/max, difference를 붙였다. **negative lag(미래 값)도 학습 feature로 넣었고**, 추론 때 범위를 벗어난 값은 NaN이 되어 median으로 채웠다. log1p, inf→NaN, median 대치, 표준화를 거쳐 LightGBM, RandomForest, XGBoost를 base로 하고 XGBoost를 meta로 쓰는 stacking을 했다. base 모델을 meta 학습과 **같은 데이터로 학습했고(OOF 아님)**, CV와 tuning은 거의 하지 않았다고 직접 적었다. 본인 평가는 corr 0.091, R² −0.112였다. public LB가 학습 기간의 마지막 90일 복사본이라 무시하고 전체 데이터로 학습했다. — [3rd place solution](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/668769)
- 6위(첫 Kaggle 대회): LSTM(입력 424 → hidden 256 → 출력 424)으로 다음 날 feature 424개를 예측하고, 이를 재귀적으로 5일 굴린 뒤 target 공식으로 변환했다. 가장 큰 개선은 최근 label lag 평균과 섞은 것이었다(α=0.7). label lag 평균만 쓰는 persistence 모델도 성능의 상당 부분을 냈다고 한다. — [Artem777 6th](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/670416)
- 7위: XGBoost와 TensorFlow DNN의 가중 앙상블(약 0.68/0.32)이고 Optuna로 함께 tuning했다. local에서는 XGBoost가 가장 좋았지만 private에서는 TF 단독이 가장 높았다. 대회 중에 API를 악용한 "fake" 제출이 있어 LB를 해석하기 어려웠다고 한다. — [Tatru 7th](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/669712)
- 8위: feature를 token으로 다루는 Transformer encoder 하나로 424개 target을 함께 예측했다. 입력은 원 feature 587개와 lag-4 target 424개(총 981)다. masked MSE, AdamW, cosine schedule을 썼다. private 점수가 갱신 때마다 0.271 → 0.306 → 0.337 → 0.490으로 올랐다. — [Transformer Based Solution](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/670493)
- 10위: ML에서 학습할 신호가 거의 없다고 보고 "regularized naive" 방식을 썼다. in-sample Kelly 최적 portfolio와 순위가 같은 예측을 만들고, 공분산 행렬은 private 기간과 길이가 비슷한 검증 구간으로 정규화했다. 늦은 검증 구간 점수는 약 0.4였다. test가 약 3개월로 짧아 운을 배제할 수 없다고 적었다. — [Regularized-naive #10](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/668589)
- 15위: 처음에는 CombinatorialPurgedGroupKFold로 학습했다. 추론 중에는 새 label이 7개 쌓일 때마다 **새 fold 모델을 추가했고 기존 모델을 덮어쓰지 않았다**. 앙상블 가중치는 validation과 test에서의 rank 점수로 정하고, fold 가중치는 성능 0.1, 최신성 0.9로 섞었다. 모델은 Attention DNN, Residual DNN, AutoEncoder이고 loss는 MSE와 Spearman을 섞었다. — [15th Place](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/668673)
- 주최 측(AlpacaTech) 발표: 설계 목표는 학습 기간과 미래 test 사이의 drift를 최소화하는 것이었다. spread target으로 시장 공통 noise를 줄였고, 1~4일 horizon을 섞어 하루에 과적합하지 않게 했다. 지표는 일별 Spearman의 평균÷표준편차("IC Sharpe")다. 상위 15개 모델을 검증해 보니 학습과 test 성능 차이가 거의 없는 모델이 대부분이었다. 참가자 수는 발표 자료에 11,387명, Kaggle 정리 글에는 등록 11,425명, 참가 1,319명, 1,126팀으로 적혀 있다. — [Speaker Deck](https://speakerdeck.com/gamella/lineyahu-detasaiensu-meetup-san-jing-wu-chan-komodeiteiyu-ce-tiyarenzi-nowu-tai-li-alpacatechpato), [Recap](https://www.kaggle.com/competitions/mitsui-commodity-prediction-challenge/discussion/669343)

### Inferences
- 1위가 해법을 공개하지 않는 경우가 금융 대회에서는 오히려 보통이다(5/5). 공개된 것 중 가장 정보가 많은 것은 2위권 영상이나 slides, 그리고 시스템을 설명한 중위권 글이다.
- Mitsui 3위는 negative lag(학습 시 미래 값), OOF 없는 stacking, 검증 없음을 스스로 밝혔다. 그런데도 3위였다는 것은 "3개월 test + 424 target" 구조에서 운의 몫이 크다는 신호다. 10위의 판단과 같다. 상위 해법이라고 해서 그대로 따라 하면 안 된다.
- GR 13위의 "classification으로 바꾸면 나빴다"는 지표가 상관(회귀)일 때의 결과다. 적중률로 평가하는 krx-quant에 그대로 옮길 수는 없지만, 비교 실험할 가치는 있다(아래 적용 메모 4번).

### Gaps
- JSRT 1·3~7위, GR 1위, JPX 1~3위, Hull 1~3위, Mitsui 1·2·4위의 해법 설명은 Kaggle 포럼, GitHub 검색, 웹 검색 어디에서도 찾지 못했다.
- JPX `shokisakai/jpx-regression` notebook의 원문 코드는 열람하지 못했다(Kaggle code 페이지는 스크립트로 그려져 내용을 가져오지 못함). "1위 추정"은 제3자의 판단이다.
- Patrick Yam의 세부 수치는 제3자가 영상과 slides를 옮긴 것이고, slides 원문은 직접 열람하지 못했다. 영상 제목만 YouTube oEmbed로 확인했다.

## 2. GTJA 191 원 보고서

### Takeaway
"GTJA 191"의 출처는 국태군안증권 금융공학팀의 2017-06-15 보고서다. 일봉 가격·거래량으로 만든 단기 alpha 191개 수식을 표 6에 모두 실었다. 보고서는 이 factor들의 유효 기간이 "대체로 1주 이내"라고 적었다.

### Cited Findings
- 제목 「基于短周期价量特征的多因子选股体系——数量化专题之九十三」(단기 가격·거래량 특징 기반 다요인 종목 선택 체계, 수량화 전문 보고서 제93호), 국태군안증권(Guotai Junan Securities) 증권연구보고서, **2017.06.15**, 분석가 李辰·刘富兵(표지 기준). 관련 보고서 목록에 같은 제목의 2017.06.01 보고서도 있다. 파일명은 2017-06-16이다. — [JoinQuant 보관 PDF](https://file.joinquant.com/198f09d54939dbcd27819bb96f67ac15/%E5%9B%BD%E6%B3%B0%E5%90%9B%E5%AE%89-%E6%95%B0%E9%87%8F%E5%8C%96%E4%B8%93%E9%A2%98%E4%B9%8B%E4%B9%9D%E5%8D%81%E4%B8%89%EF%BC%9A%E5%9F%BA%E4%BA%8E%E7%9F%AD%E5%91%A8%E6%9C%9F%E4%BB%B7%E9%87%8F%E7%89%B9%E5%BE%81%E7%9A%84%E5%A4%9A%E5%9B%A0%E5%AD%90%E9%80%89%E8%82%A1%E4%BD%93%E7%B3%BB-2017-06-16.pdf)
- 본문 3.3절: "总共构建了191个基于价格和成交量数据的短周期阿尔法因子"(일봉 데이터). 표 6에 Alpha1(`-1*CORR(RANK(DELTA(LOG(VOLUME),1)), RANK((CLOSE-OPEN)/OPEN), 6)`)부터 Alpha191까지 수식이 있고, 함수 정의는 부록 2에 있다. — [같은 PDF](https://file.joinquant.com/198f09d54939dbcd27819bb96f67ac15/%E5%9B%BD%E6%B3%B0%E5%90%9B%E5%AE%89-%E6%95%B0%E9%87%8F%E5%8C%96%E4%B8%93%E9%A2%98%E4%B9%8B%E4%B9%9D%E5%8D%81%E4%B8%89%EF%BC%9A%E5%9F%BA%E4%BA%8E%E7%9F%AD%E5%91%A8%E6%9C%9F%E4%BB%B7%E9%87%8F%E7%89%B9%E5%BE%81%E7%9A%84%E5%A4%9A%E5%9B%A0%E5%AD%90%E9%80%89%E8%82%A1%E4%BD%93%E7%B3%BB-2017-06-16.pdf)
- 보고서가 말하는 특징: factor 유효 기간은 대체로 1주 이내이고, 매매는 T+2일 주기로 했다. 연간 양방향 turnover는 30~50배, 보유 종목은 50~60개다. 2012-01~2017-04 거래비용을 뺀 뒤 CSI 500 대비 연 초과수익 50.2%, MDD 5.9%, IR 4.67이었다. 견딜 수 있는 거래비용 한계는 양방향 약 1%였다(보고서 자체 수치이며 검증하지 않음). — 같은 PDF
- 예측 horizon별 factor 연 수익 평균: 1일 6.85%, 2일 8.08%, 3일 6.09%, 4일 5.42%, 5일 4.78%. 191개 전체로 일별 예측한 IC 평균은 0.057이었다. factor 수를 20 → 191로 늘리자 모델 IC가 0.0338 → 0.057로 단조 증가했다. — 같은 PDF 표 7, 표 10
- 2026년 논문은 미국 주식에서 기본 factor 151개를 통제한 뒤에도 Alpha191 중 17개가 5% 수준에서 증분 설명력을 가진다고 보고했다. — [arXiv 2601.06499](https://arxiv.org/abs/2601.06499v1)

### Inferences
- 191개 대부분은 종목 간 `RANK`, `CORR`과 일봉 window로 되어 있다. 3종목 4분 horizon에는 그대로 옮길 수 없다. "약한 factor를 많이 모으면 IC가 단조 증가한다"는 결과도 수천 종목에 대한 횡단면 결과다.

### Gaps
- 같은 제목의 2017-06-01 보고서와 06-15 보고서가 어떻게 다른지는 확인하지 못했다.

## 3. pandas-ta 404와 대체 도구

### Takeaway
원 저장소가 사라진 이유를 밝힌 공식 공지는 찾지 못했다. 확인된 현상은 네 가지다. GitHub 저장소 404, 소유 계정의 공개 저장소 0개, PyPI의 0.3.x 버전 삭제, 공식 사이트 접속 불가. 2026년에 다른 프로젝트들은 pandas-ta-classic(import 이름이 바뀜) 또는 freqtrade 팀의 ft-pandas-ta(import 이름 그대로)로 옮기고 있다. krx-quant라면 TA-Lib나 numpy 직접 구현이 가장 안전하다.

### Cited Findings
- `api.github.com/repos/twopirllc/pandas-ta`는 404다. 계정 `twopirllc`(이름 2PIR)는 남아 있지만 public_repos가 0이고 updated_at은 2026-06-30이다. — [GitHub API user](https://api.github.com/users/twopirllc)
- PyPI `pandas-ta`에는 0.4.67b0(2025-09-03)과 0.4.71b0(2025-09-14) 두 버전만 남아 있다. `0.3.14b0` JSON은 404다. 0.4.71b0은 Python ≥3.12를 요구하고, licence 칸은 비어 있으며, Repository는 여전히 404인 GitHub 주소를 가리킨다. Homepage `pandas-ta.dev`는 2026-10-17 DNS 조회에 실패했다. — [PyPI JSON](https://pypi.org/pypi/pandas-ta/json)
- 사용자 이슈: "pandas-ta==0.3.14b0 can no longer be installed". 원 GitHub 저장소는 404이고, 0.4.71b0은 `pandas>=2.3.2`와 numba를 요구해 drop-in 대체가 아니다. 0.3.14b0은 numpy 2에서 `from numpy import NaN` 때문에 import가 실패한다. 이 사용자는 freqtrade fork로 문제를 해결했다. — [july-backtester#415](https://github.com/zachisit/july-backtester/issues/415)
- 다른 이슈: 0.4.71b의 메타데이터가 404 저장소를 가리키므로 "provenance of this release cannot be verified on GitHub". 권고는 pandas-ta-classic으로 옮기는 것이다(import `pandas_ta_classic`). — [TradeMaster#236](https://github.com/TradeMaster-NTU/TradeMaster/issues/236), [p-zombie/freqtrade#13](https://github.com/p-zombie/freqtrade/issues/13)
- 대체 1: [xgboosted/pandas-ta-classic](https://github.com/xgboosted/pandas-ta-classic). README에 "classic/community maintained version"이라고 적혀 있다. MIT, star 457, push 2026-10-13, PyPI 0.8.32(2026-09-14). — [PyPI](https://pypi.org/project/pandas-ta-classic/)
- 대체 2: [freqtrade/pandas-ta](https://github.com/freqtrade/pandas-ta). 0.3.x 코드의 fork이고 import 이름이 `pandas_ta` 그대로다. MIT, star 24, PyPI `ft-pandas-ta` 0.3.16(2025-09-29). — [PyPI](https://pypi.org/project/ft-pandas-ta/)
- 대체 3: TA-Lib PyPI 0.8.2(2026-10-12, 앞 노트 표). `ta`(bukosabino) 0.11.0은 2023-11 이후 release가 없다. — [PyPI ta](https://pypi.org/project/ta/)

### Inferences
- PyPI에 남은 0.4.x를 누가 올렸는지 GitHub에서 확인할 수 없다. 출처 검증이 안 되는 패키지를 서버 의존성으로 넣는 것은 공급망 위험이다.

### Gaps
- 저장소를 삭제했는지, 비공개로 돌렸는지, 상용화했는지, 그 이유는 무엇인지 1차 출처를 찾지 못했다(web.archive.org는 이 환경에서 열 수 없었다).

## 4. 앞 노트가 놓친 2023–2026 시장 예측 대회

### Takeaway
가장 중요한 새 대회는 2026-07~10 Kaggle community 대회 "MSCapital – Real Financial Market Forecasting"이다. 중국 A주 약 1,800종목의 3초 snapshot과 역체결 데이터로 1·5·30분 수익률 가중합을 예측했다. 자기 종목 이력만 쓴 LOB 모델의 점수는 cosine 약 0.13(private)에 그쳤다. 그 위의 점수는 거의 전부 표본 간 시간 정렬 leak에서 나왔다. 짧은 horizon 종목 수익률의 상당 부분이 "시장이 어디로 가느냐"라는 점을 보여 준다.

### Cited Findings

**MSCapital – Real Financial Market Forecasting (Kaggle community, 2026-07-07 ~ 2026-10-09, 287팀)**
- 주최 계정 표시명 "Rib~"(dc5e964768ef56302a32)는 JSRT 1위 팀 "ms capital"의 팀원 계정과 같다. 상위 10팀에게 재현용 해법 문서를 제출하라고 공지했다. 최종 1위 0.418 / 2위 0.371 / 3위 0.368(private). — [Leaderboard](https://www.kaggle.com/competitions/ms-capital-real-financial-market-forecasting/leaderboard), [주최 공지](https://www.kaggle.com/competitions/ms-capital-real-financial-market-forecasting/discussion/747785), [JSRT Leaderboard](https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/leaderboard)
- 7위 senkin13의 정리. 입력은 예측 시점 전 약 600초의 snapshot(약 3초 간격, 1·2호가, bar 안 체결 요약)과 직전 60초의 주문·체결 이벤트다. 가격과 종목, 날짜는 익명화돼 있다. 학습 1,257,637개(71개 "월"), test 647,896개. 지표는 uncentered cosine이다. target은 대략 `0.9·r(1분) + 0.45·r(5분) + 0.15·r(30분)`이다. 표본 간 정보를 쓰지 않은 표 + sequence 앙상블은 public 0.164 / private 0.129였다. 핵심은 snapshot 시각의 ms jitter "barcode"로 다른 종목의 같은 시각 표본을 맞추는 것이었다. 그러면 창이 늦게 열린 표본에서 다른 표본의 label 구간 시장 경로가 보인다. 이것으로 0.372/0.337까지 올렸다. — [7th place solution](https://www.kaggle.com/competitions/ms-capital-real-financial-market-forecasting/discussion/747789)
- 같은 글의 leak 외 기법: 기본 feature 약 1,157열(호가 압력, 최근 주문 흐름, Kyle λ, 자기상관, variance ratio 등). 가장 좋은 단일 모델은 XGBoost pseudo-Huber(slope 0.005)였다. sequence 학생 모델에는 **시장 반전(flip) augmentation**(가격 부호 반전 + 매수/매도 교환)과 flip TTA `0.5·ŷ − 0.5·ŷ(mirror)`를 썼다. "flip 외의 augmentation은 모두 해로웠다. 실제로 존재하는 대칭만 쓸모 있었다." 앙상블 가중치는 NNLS를 "월 홀짝 교차"로 맞췄다. 전역 calibration, scaling, rank blending은 효과가 없었다. 전체 kNN target encoding은 월 단위 CV에서 label이 새서 제외했다. 초기 월을 뺀 모델은 다양성 원천으로 유효했다. — 같은 글
- 2위(简单线性回归 팀): 자기 이력만 쓴 모델은 public 0.165 / private 0.128, 첫날 LightGBM은 0.092 / 0.074였다. 나머지 점수는 test 표본 순서(`numpy RandomState(1).permutation`) 역산, 예측 시각을 만든 난수(`random.Random(월번호)`) 역산 같은 데이터 생성 과정 해독에서 나왔다. 가격 제한(상·하한가)이나 빈 호가 표본은 label의 94%가 정확히 0인데 모델들이 약 6배 과대 예측해서 따로 처리했다. 업종, 스타일, 종목 residual factor는 ±0.001 이내로 효과가 없었다. 학습 월을 늘리자 local은 +0.012였지만 public에서는 유지되지 않았다. — [2nd place solution](https://www.kaggle.com/competitions/ms-capital-real-financial-market-forecasting/discussion/748874)

**그 밖에 Kaggle에서 확인한 2023–2026 금융 대회**(Kaggle 대회 목록 검색. 80팀 이상, 상금 표시 기준)
- Featured(상금 대회)는 앞 노트에 있는 Optiver TATC, JSRT, Hull 세 개다. Community 대회로 "Hedge fund – Time series forecasting"(2026-01~04, 982팀, $10,000, Skill Score)이 있지만 해법 글은 없다. "Detecting Reversal Points in US Equities"(2025-09~12, 426팀, $3,500, 익명 주가의 local high/low 분류)도 있다. — [ts-forecasting](https://www.kaggle.com/competitions/ts-forecasting/leaderboard), [detecting-reversal-points](https://www.kaggle.com/competitions/detecting-reversal-points-in-us-equities)

**Kaggle 밖**
- ADIA Lab Market Prediction(CrunchDAO, 2023): 날짜마다 투자 universe의 종목 순위를 매기는 과제다. 익명 feature 0~460, target은 [−1, 1]로 바꾼 미래 성과 순위, 지표는 Spearman이다. 2023-05~08에 제출하고 08~11월 실데이터로 out-of-sample 평가를 했다. 1위 상금은 $40,000이다. 문서에 우승자나 해법은 없다. — [CrunchDAO docs](https://docs.crunchdao.com/competitions/competitions/adia-lab-market-prediction-competition)
- Numerai Signals: 참가자가 자기 데이터로 만든 종목별 0~1 값을 제출한다. Numerai가 이미 가진 signal에 대해 neutralize한 뒤 평가한다. "Numerai Signals is not about predicting stock returns, it is about finding original signals". 진단 지표는 `target_jupiter_60`(60일, 2일 lag) 기준 neutral correlation과 BMC다. 문서는 "It's trivial to overfit a signal on validation"이라고 경고한다. — [Signals overview](https://docs.numer.ai/numerai-signals/signals-overview)
- ML Contests 2025 보고서는 금융 대회로 JSRT만 다뤘고, 우승 기법은 적지 않았다. — [ML Contests 2025](https://mlcontests.com/state-of-machine-learning-competitions-2025/)

**한국 금융 예측 대회(DACON)**
- 제2회 KRX 주식 투자 알고리즘 경진대회(주최 한국거래소, 협찬 코스콤, 주관 데이콘, 2023). 예선 규정: 2,000종목 중 상위 200 long, 하위 200 short, 동일 금액, 종가에 사고 다음 날 종가에 판다. private는 2023-07-28 기준 예측, 2023-07-31~08-21 모의 거래, 15거래일 Sharpe(무위험 3.5%)로 순위를 정했다. — [평가 규정 공지](https://dacon.io/en/competitions/official/236117/talkboard/408727), [대회 개요](https://dacon.io/en/competitions/official/236117/overview/description)
- 같은 대회 "Overall Ranking" 1~3위: kcj98 0.59925, KAIK 0.54371, 25478432 0.53428(public인지 private인지 페이지에 표시 없음). — [Leaderboard](https://dacon.io/en/competitions/official/236117/leaderboard)
- KAIK(위 표 2위)의 공개 코드: 9년치 일봉과 유동성으로 **종목별 Sharpe를 OLS로 예측**했고 mean reversion을 가정했다. 검증 방법은 적혀 있지 않다. — [KAIK codeshare](https://dacon.io/en/competitions/official/236117/codeshare/8608)
- 주식 종료 가격 예측 경진대회(데이콘, 2021로 추정): KOSPI200·KOSDAQ150 종가 회귀, NMAE, 상금 3,000만 원, 완료 703명. — [대회 개요](https://dacon.io/en/competitions/official/235857/overview/description)
- 2021 NH투자증권 빅데이터 경진대회(주식 보유기간 예측)가 있었다. 상세는 확인하지 않았다. — [대회 페이지](https://dacon.io/en/competitions/official/235798/overview/description)
- 미래에셋증권 "제9회 AI 페스티벌"(2025, 443팀, 결선 6팀)은 금융과 AI 아이디어 대회로 보도됐다. 가격 예측 대회인지는 확인하지 못했다. — [스마트투데이](https://www.smarttoday.co.kr/ko-kr/articles/93789)

### Inferences
- MSCapital의 자기 이력 모델 점수(private cosine 약 0.13)는 A주 1,800종목, 1~30분 horizon에서 LOB와 주문 흐름 feature만으로 얻는 현실적인 상한에 가깝다. 그 위의 점수는 "같은 시각 다른 종목의 미래" 같은 정보에서 나왔다. 실시간 엔진에서는 쓸 수 없는 정보다. 다만 krx-quant에서 같은 정보에 해당하는 것은 **선행하는 시장 변수(KOSPI200 선물 등)의 다음 움직임**이고, 이것이 종목 4분 방향을 크게 좌우할 가능성이 높다.
- DACON KRX 대회는 15거래일 Sharpe로 순위를 매겼다. Hull, JPX와 같은 구조라 운의 영향이 크다(앞 노트 JPX의 랜덤 점수 σ 논의와 같다).

### Gaps
- MSCapital의 최종 리더보드 검증 여부와 상금 유무는 확인하지 못했다(community 대회, 2026-10-09 마감). 1위 해법은 2026-10-17 현재 없다.
- ADIA Lab Market Prediction의 우승자와 해법, DACON KRX 대회 1위(kcj98)의 해법은 찾지 못했다.
- 한국투자증권과 키움증권이 주최한 AI 예측 대회는 찾지 못했다.

## 5. 한국 퀀트 자료와 커뮤니티

### Takeaway
검증 가능한 한국 자료는 공식 API 포털(KIS, 키움 REST, OpenDART, KRX 정보데이터시스템), 오픈소스 라이브러리, DACON 대회 페이지, 책 예제 저장소다. 퀀트 전용 커뮤니티(네이버 카페 등)는 이번에도 신뢰할 만한 1차 출처를 확보하지 못했다.

### Cited Findings

| 자료 | 내용 | 상태(2026-10-17) |
|---|---|---|
| [KIS Developers](https://apiportal.koreainvestment.com) | 한국투자증권 오픈API 개발자센터 | 접속 확인 |
| [키움 REST API](https://openapi.kiwoom.com) | 키움증권 REST API 포털(제목 "키움 REST API") | 접속 확인 |
| [OPENDART](https://opendart.fss.or.kr) | 금융감독원 전자공시 API | 접속 확인 |
| [KRX 정보데이터시스템](https://data.krx.co.kr) | 거래소 공식 통계 | 접속 확인 |
| [FinanceData/OpenDartReader](https://github.com/FinanceData/OpenDartReader) | OpenDART Python wrapper | MIT, star 479, push 2026-08-10, PyPI 0.3.3(2026-08-10) |
| [FinanceData/FinanceDataReader](https://github.com/FinanceData/FinanceDataReader) | 국내외 가격·지수·종목 목록 | MIT, star 1,555, PyPI 0.9.202(2026-05-13) |
| [sharebook-kr/pykrx](https://github.com/sharebook-kr/pykrx) | KRX와 Naver scraping | star 1,105, push 2026-09-30, PyPI 1.2.9(MIT 표기) |
| [Soju06/python-kis](https://github.com/Soju06/python-kis) | KIS REST 기반 trading 라이브러리 | MIT, star 288, PyPI 2.1.6(2025-10-13) |
| [sharebook-kr/mojito](https://github.com/sharebook-kr/mojito) | KIS API wrapper | MIT, push 2024-02, PyPI `mojito2` 0.1.6(2023-02). 관리가 멈춘 것으로 보임 |
| [sharebook-kr/pykiwoom](https://github.com/sharebook-kr/pykiwoom) | 키움 OpenAPI+ (Windows COM) wrapper | Apache-2.0, push 2025-07 |
| [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api) | KIS 공식 샘플 | star 1,649, push 2026-10-14(앞 노트) |

- 책 예제: 이현열 「R을 이용한 퀀트 투자 포트폴리오 만들기(개정판)」는 온라인 본문이 공개돼 있다([사이트](https://hyunyulhenry.github.io/quant_cookbook/), [저장소](https://github.com/hyunyulhenry/quant_cookbook), CC0-1.0). 「파이썬을 이용한 퀀트 투자 포트폴리오 만들기」는 저장소만 있다([hyunyulhenry/quant_py](https://github.com/hyunyulhenry/quant_py), star 270, push 2024-05). GitHub Pages 주소는 "Site not found"였다.
- 퀀티랩 「파이썬을 이용한 딥러닝/강화학습 주식투자 – 파이토치와 케라스를 활용한 인공지능 퀀트 투자 시스템(개정2판)」 예제다. 환경은 TF 2.7과 PyTorch 1.10 기준으로 오래됐다. — [quantylab/rltrader README](https://github.com/quantylab/rltrader)
- 강환국 「할 수 있다! 퀀트 투자」(에프엔미디어, 2017-09)는 재무제표와 가격 지표 기반의 규칙형 factor 투자 입문서다(장기 투자 중심). — [교보문고 eBook](https://ebook-product.kyobobook.co.kr/dig/epd/ebook/E000002997763)
- DACON(데이콘)은 국내 AI 경진대회 플랫폼이다. 금융 대회는 위 4절 참고. — [dacon.io](https://dacon.io)

### Inferences
- 4분 horizon의 호가 기반 엔진에 직접 쓸 만한 한국 자료는 공식 API와 거래소 데이터 정도다. 한국 퀀트 서적과 커뮤니티는 대부분 일봉 이상 factor 투자가 주제다.
- 키움이 REST API를 내놓았다. Windows COM 기반 OpenAPI+에 묶이지 않는 데이터 경로가 하나 더 생긴 셈이다(엔진 데이터 경로를 바꿀지는 별도 판단).

### Gaps
- 네이버 카페, 개인 블로그, 한국어 퀀트 YouTube는 검증 가능한 링크를 확보하지 못했다. systrader79 위키독스 자료는 403이라 내용을 확인하지 못했고([wikidocs.net/book/16915](https://wikidocs.net/book/16915)), 강환국 YouTube는 책 소개 문구로만 언급을 확인했다.
- 키움 REST API의 실시간 호가와 체결 제공 범위, 이용 조건은 확인하지 않았다.

## 6. 그 밖의 미확인 항목 정리

### Takeaway
앞 노트에서 미확인으로 남았던 도구 사실은 모두 확인했다.

### Cited Findings
- Qlib Alpha158 기본 구성: `kbar`(KMID, KLEN, KMID2, KUP, KUP2, KLOW, KLOW2, KSFT, KSFT2 = 9), `price` window [0]의 OPEN/HIGH/LOW/VWAP 4개, `rolling` 연산자 29개 × window [5, 10, 20, 30, 60] = 145. 합계 158이다. — [loader.py](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)
- LEAN `Resolution` enum: Tick, Second, Minute, Hour, Daily. — [Common/Global.cs](https://github.com/QuantConnect/Lean/blob/master/Common/Global.cs)
- zipline-reloaded CLI `--data-frequency`: `daily`/`minute`(기본 daily). tick은 지원하지 않는다. — [src/zipline/__main__.py](https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/__main__.py)

### Inferences
- 결론은 앞 노트와 같다. LEAN은 tick을 지원하지만 체결 시뮬레이터라서 signals-only 엔진에는 필요 없다.

### Gaps
- 없음.

## krx-quant 적용 메모 (앞 노트에 없던 것만)

1. **Flip 대칭 TTA와 augmentation 검증**(MSCapital 7위): 가격 변화의 부호를 뒤집고 매수/매도(OFI, 잔량 불균형, 체결 방향)를 맞바꾼 mirror 입력을 만든다. 그 입력의 예측에서 p(up)과 p(down)을 바꾼 값을 원래 예측과 평균한다. up/down 확률의 대칭성을 강제하는 셈이다. KRX에는 비대칭 요인(공매도 제약, 상·하한가, 하락 시 변동성 증가)이 있으므로 "실제로 존재하는 대칭만 도움이 된다"는 경고대로 `replay`에서 적중률을 비교하고 채택한다. 학습 데이터를 두 배로 늘리는 augmentation은 2 GB 메모리를 생각하면 TTA부터 해 본다.
2. **최근 날짜 가중 sample weight**(JSRT 2위): `(c + d)/(c + d_max)` 꼴의 선형 recency weight. nightly 재학습 window 안에서 최근 거래일을 더 무겁게 둔다. c(오래된 날의 바닥 가중)는 walk-forward로 정한다.
3. **seed마다 early stopping하지 않기**(JSRT 2위): seed 평균을 쓸 때 `num_boost_round`를 seed마다 early stopping으로 고르지 말고 하나로 고정한다. seed별 best round 선택은 noise였다.
4. **"회귀 후 임계값" vs "직접 3-class"를 비교**(GR 13위는 classification이 나빴다고 보고): 4분 수익률(또는 barrier 도달 방향)을 회귀나 pseudo-Huber(MSCapital 최강 단일 모델)로 예측한 다음, up/flat/down 임계값으로 자른 결과의 적중률을 지금의 LightGBM multiclass와 비교한다.
5. **나눠서 이어 학습하기와 행 표본 추출로 메모리 줄이기**(JSRT 9위): 날짜마다 행의 40~50%만 뽑아도 검증 점수가 거의 같았다. LightGBM은 `init_score`/`init_model`로 boosting을 여러 번에 나눠 이어 학습할 수 있다. nightly 학습의 피크 메모리를 2 GB 안에 두는 수단이다.
6. **ensemble 가중치를 교차 적합하기**(MSCapital 7위의 월 홀짝 교차): log-loss 가중치를 같은 구간에서 맞추고 평가하지 말고, 홀수 거래일로 맞춰 짝수일에 평가하고 그 반대도 한다. 그렇게 해서 가중치가 과적합됐는지 본다.
7. **모델을 덮어쓰지 말고 쌓기 + 최신성 가중**(Mitsui 15위): 매일 밤 새 모델을 추가하고 최근 k개를 남긴다. 가중치는 최근 성과와 최신성을 섞어 정한다(그 글은 0.1:0.9). 지금의 log-loss 가중 앙상블에 "나이" 항을 더하는 형태다. 메모리 한도 안에서 k를 정한다.
8. **특수 상태 표본은 따로 처리**(MSCapital 2위: 상·하한가와 빈 호가 표본은 label의 94%가 0): KRX의 VI(변동성 완화장치) 발동 뒤 단일가 구간, 상·하한가 근접, 동시호가 구간 표본은 학습과 평가에서 분리하거나 flat으로 고정한다. 그렇지 않으면 정상 구간의 확률이 왜곡될 수 있다.
9. **persistence 기준선 추가**(Mitsui 6위): 최근 N개 4분 구간의 실현 class 빈도(또는 최근 label 평균)만으로 낸 예측을 majority-class 기준선 옆에 둔다. 모델이 이 기준선을 넘는지 `prevalidate`에서 확인한다.
10. **시장 성분 예측에 우선 투자**(MSCapital 자기 이력 모델 상한 약 0.13): 종목의 1~30분 수익률은 시장 경로가 크게 설명한다. 종목 고유 LOB feature를 더하기보다 KOSPI200 선물의 다음 4분 방향을 예측하고(lead-lag 모델 강화) 종목별 β로 반영하는 쪽이 이득이 클 가능성이 높다. 검증 필요.
11. **leak 체크 항목 추가**: target encoding이나 kNN feature를 전체 기간으로 계산하면 기간 단위 CV에서 label이 샌다(MSCapital 7위). "negative lag" feature는 학습 때만 미래 값을 본다(Mitsui 3위). 둘 다 금지 목록에 넣는다.
