# 리서치

엔진 설계 밖의 조사 자료를 둔다. 결정이 아니라 근거 모음이다. 결정이 되면 `docs/`와 이슈로 옮긴다. 투자 조언이 아니다.

## 단기 매매 퀀트 기법 조사 (2026-10-17)

스캘핑, 데이 트레이딩, 스윙 트레이딩에 쓰이는 퀀트 기법, 유명 퀀트 회사, 근거 논문을 주제 10개로 나눠 조사했다.

- 종합 보고서: [reports/단기 매매 퀀트 기법 조사.md](reports/단기%20매매%20퀀트%20기법%20조사.md)
- 주제별 노트 (`research_notes/단기 매매 퀀트 기법 조사/`):

| 파일 | 주제 |
|---|---|
| `01_microstructure_order_flow.md` | 호가·체결 신호(OFI, queue imbalance, microprice, Hawkes, VPIN)의 예측력과 감쇠 |
| `02_hft_market_making_scalper_economics.md` | HFT·market making 모델, latency arbitrage, 개인 day trader 성과 연구 |
| `03_intraday_day_trading.md` | 장중 momentum, 시간대 periodicity, ORB, overnight·장중 수익률, 기술적 규칙 검증 |
| `04_swing_trading.md` | 단기 반전, momentum·TSMOM, pairs·stat-arb, PEAD, volatility-managed, 한국 수급 |
| `05_ml_deep_learning.md` | DeepLOB·LOBCAST, 횡단면 ML, triple barrier·meta-labeling, foundation model, LLM, RL |
| `06_volatility_regime.md` | HAR·GARCH·range 추정, 점프, HMM·jump model·change-point, volatility targeting, IV |
| `07_quant_firms.md` | Renaissance, Jane Street, Citadel 등 24개 회사 프로필과 한국 사례 |
| `08_korea_market.md` | 2026년 KRX·NXT 규칙과 비용, 인버스 ETF, 투자자 수급, 선물 선행, 미국 연동 |
| `09_backtesting_validation.md` | 과적합(PBO, DSR), 다중검정, 방향 적중 검정, 비용 모델 |
| `10_competitions_opensource.md` | Kaggle 시장 예측 대회 상위 해법, 오픈소스 도구 현황 |

보고서의 권고에서 나온 제안 이슈(`needs-triage`): #262 채점 보강, #263 방향 예측의 지평 사다리(15·30·60분), #264 하루 한 번 예측(다음날 시가 갭, 시가→종가, 마감 30분), #265 같은 시간대 수익률 피처, #266 meta-labeling, #267 하락 예측의 인버스 비용.

노트마다 확인하지 못한 수치와 출처는 (미확인)·(미검증) 등으로 표시했다. 조사 중 웹 검색 한도가 일부 노트(04, 08, 10)에서 먼저 바닥나, 그 노트의 빈 곳은 각 노트의 Gaps 절에 적었다.
