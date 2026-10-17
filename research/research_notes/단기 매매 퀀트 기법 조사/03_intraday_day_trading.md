# 장중(day trading) 퀀트 전략: 발표된 증거 정리 (2026-10-17 기준)

> 범위: 몇 분~몇 시간 보유하고 장 마감 전에 청산하는 전략. 투자 권유가 아니라 연구 정리다.
> 표기: **[오래됨]**은 표본이 2000년대 초 이전인 결과. **[미확인]**은 원문을 열어 보지 못했거나 2차 자료만 본 내용. **[비동료심사]**는 블로그, 업계 자료, working paper.

## 0. 전략 카탈로그 (한눈에 보기)

### Takeaway
학술 증거가 가장 단단한 것은 **시장 수준 intraday momentum(첫 30분 또는 하루의 나머지 → 마지막 30분)**, **같은 시간대 periodicity(HKS)**, **개별 종목의 end-of-day reversal**, **overnight/intraday 분리**다. ORB와 noise-area 전략은 업계 저자의 working paper이고 비용을 넣은 독립 재현에서 결과가 갈린다. 기술적 규칙은 data-snooping과 비용을 보정하면 대부분 사라진다. 한국에서 시장 수준 intraday momentum은 **약하다**. 같은 시간대 periodicity(장 마감 무렵 지속, 개장 직후 반전)는 한국에서도 확인됐다.

### Cited Findings (카탈로그)

| 전략 | 규칙 (1~2줄) | 핵심 증거 (시장, 기간, 결과) | 비용 반영 | 알려진 실패 양상 | 미국 밖 검증 |
|---|---|---|---|---|---|
| Market intraday momentum | 첫 30분 수익률(+12번째 30분)의 부호대로 마지막 30분 매매 | SPY 1993–2013, 표본 내 R² 1.6%, 표본 외 R² 1.69%, 연 6.67%, Sharpe 1.08, 적중률 54.37% (항상 매수 50.42%) — [Gao et al. WP](https://papers.ssrn.com/abstract=2440866) | 저자 주장: 비용 뒤에도 certainty-equivalent 이득 유의 | 2007–09 위기를 빼면 R² 0.7%로 약해짐 | 16개 선진시장 중 12개 유의 — [Li et al. 2022](https://ideas.repec.org/a/eee/finmar/v57y2022ics138641812100001x.html). **한국 ETF는 약함** — [Limkriangkrai et al. 2023](https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html) |
| Hedging-demand momentum | 하루의 나머지(ROD) 수익률 → 마지막 30분. 딜러 short gamma와 레버리지 ETF 리밸런싱이 원인 | 60개 넘는 선물, 1974–2020, 모든 자산군에서 확인 — [Baltussen et al. 2021](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365) | — | 효과가 이후 며칠 동안 되돌아감. 딜러가 long gamma면 약해짐 | 여러 나라 지수선물 포함 (KOSPI200 포함 여부 **[미확인]**) |
| Noise-area 추세 ("Beat the Market") | 시가 ± 최근 14일 같은 시각의 평균 \|시가 대비 변동\|을 넘으면 추세 방향 진입, 동적 trailing stop, 장 마감 청산 | SPY 2007–2024 초, 연 19.6%, Sharpe 1.33 (수수료와 slippage 반영) — [Zarattini, Aziz, Barbon 2024](https://papers.ssrn.com/abstract=4824172) | 저자: 반영 | 독립 재현 못 찾음 **[비동료심사]** | 없음 |
| ORB (지수 ETF) | 첫 5분봉이 양봉이면 매수, 음봉이면 매도, 장 마감 청산 | QQQ 2016–2023, 연 alpha 33% (수수료 반영), TQQQ로 1,484% — [Zarattini & Aziz 2023](https://papers.ssrn.com/abstract=4416622) | 수수료만. spread와 slippage 없음 | 지수 5개 재현: 총수익은 재현되나 비용 뒤 0과 구분 안 됨 — [MQL5 블로그](https://www.mql5.com/en/blogs/post/776235) **[비동료심사]** | 없음 |
| ORB (stocks in play) | 첫 5분 거래량이 14일 평균보다 큰 상위 20개 종목에서 5분 ORB | 미국 7,000여 종목 2016–2023, 순수익 1,600% 이상, Sharpe 2.81 — [Zarattini, Barbon, Aziz 2024](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/) | 저자: net | QuantConnect 재현은 Sharpe 2.4 — [QuantConnect](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/) | 없음 |
| Intraday periodicity | 같은 30분(또는 10분) 구간의 과거 수익률 → 오늘 같은 구간 (지속). 직전 구간끼리는 반전 | 미국 주식 30분 수익률, 지속 효과가 40거래일 이상 감 — [Heston, Korajczyk, Sadka 2010](https://arxiv.org/abs/1005.3535) | 없음 | 1시간 미만 lag에서는 반전(bid-ask bounce) | **한국 KOSPI200 2016–2021: 장 마감 30분 지속, 09:00–09:10 반전** — [김찬수 외 2022](https://doi.org/10.5762/KAIS.2022.23.7.344) |
| End-of-day reversal (종목 횡단면) | 오늘 전일 종가~15:00(미국 기준 close−60분) 수익률이 낮은 종목을 사고 높은 종목을 팔아 마지막 30분 보유 | 미국 1993–2019, 하루 3.78bp(VW)~6.86bp(EW), 연 9.5~17.3% (총수익) — [Baltussen, Da, Soebhag 2025 WP](https://academicweb.nd.edu/~zda/EOD.pdf) | 저자: 많은 투자자에게 비용 뒤 이용은 어려울 수 있음 | 시장 수준 momentum과 방향이 반대 | 없음 |
| Overnight/intraday 분리 | 종가 매수 → 시가 매도(overnight) 대 시가 → 종가 | QQQQ 1999–2006 overnight premium 평균 약 +23.7%, 장중 약 −23.3% — [Kelly & Clark 2011](https://ideas.repec.org/a/pal/assmgt/v12y2011i2d10.1057_jam.2011.2.html) **[오래됨]** | 없음 | 회전율이 높아 비용에 민감 | 한국: overnight와 장중 수익률이 음의 관계 — [Ham et al. 2023](https://ideas.repec.org/a/eee/finlet/v54y2023ics1544612323001526.html) |
| 갭 반전 (gap fade) | 큰 시가 갭이나 개장 직후 큰 움직임의 반대 방향 | S&P500·항셍 선물: 큰 개장 움직임이 부분 반전, 비용 뒤 소폭 이익 — [Fung, Mok, Lam 2000](https://scholars.hkbu.edu.hk/en/publications/intraday-price-reversals-for-index-futures-in-the-us-and-hong-kon/) **[오래됨]** | 반영 | 15년 미국 재검증에서 비용이 이익을 크게 깎음 — [Grant, Wolf, Yu 2005](https://ideas.repec.org/a/eee/jbfina/v29y2005i5p1311-1327.html) | 홍콩 (위 논문). 한국 ETF: overnight +, 장중 − — [이상기·홍정훈 2022](https://ssu.scholarworks.kr/handle/2018.sw.ssu/42155) |
| 기술적 규칙 (장중) | 5분봉 filter, MA, S/R, channel breakout, OBV | SPY 5분 2002–2003, 7,846개 규칙 — [Marshall, Cahan, Cahan 2008](https://ideas.repec.org/a/eee/empfin/v15y2008i2p199-210.html) **[오래됨]** | — | data snooping 보정 뒤 유의성이 사라진다는 것이 초록 결론으로 기억되나 **[미확인]** | 도쿄 Nikkei225 종목: OFI와 호가 불균형이 단기 수익률을 예측하지만 어떤 전략도 buy-and-hold를 못 이김 — [Yamamoto 2012](https://ideas.repec.org/a/eee/jbfina/v36y2012i11p3033-3047.html) |

### Inferences
- 증거 등급은 대략 이 순서다: (A) Gao, Baltussen, HKS, Bogousslavsky, LPS (JF/JFE, 긴 표본) → (B) EOD reversal (working paper, 엄밀함) → (C) Zarattini 계열 (업계 저자 SSRN, 독립 재현이 엇갈림).
- 한국의 세 종목(005930, 000660, 009150)은 미국 대형주와 비슷한 고유동성 종목이다. 그래도 한국에서는 시장 수준 momentum이 약하다는 결과가 있으므로, 미국 결과를 그대로 옮기지 말고 검증해야 한다.

### Gaps
- Toby Crabel의 ORB 원전(1990년 책)은 이번에 서지를 확인하지 못했다.
- [Holmberg, Lönnbark, Lundström 2013](https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html) (ORB, Finance Research Letters)은 초록만 확인했다. 대상 시장과 결과 수치는 **[미확인]**이다.

---

## 1. Intraday momentum: Gao et al. (2018), Baltussen et al. (2021), Zarattini et al. (2024)와 아시아 재현

### Takeaway
미국에서는 첫 30분(또는 하루의 나머지)이 마지막 30분을 예측한다. R²는 1~3%이고 적중률은 약 54%다. 고변동성 날과 딜러 short gamma 날에 강하다. 아시아에서는 중국과 일본에서 뚜렷하고, **한국은 약한 증거만 있다**. 0DTE 시대(2022~) 미국에서 무조건부 효과가 약해졌다는 주장이 있다.

### Cited Findings
- **Gao, Han, Li, Zhou (2018)**, JFE 129(2):394–414 ([DOI](https://doi.org/10.1016/j.jfineco.2018.05.009), [SSRN WP](https://papers.ssrn.com/abstract=2440866)): SPY 1993-02~2013-12. 첫 30분 수익률이 마지막 30분 수익률을 예측한다. 다른 활발한 ETF 10개에서도 확인됐다. 변동성과 거래량이 높은 날, 경기침체기, 일부 거시지표 발표일에 더 강하다 — [WP 초록](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf)
  - 수치: 표본 내 R² 1.6%, 12번째 30분을 더하면 2.6%. 표본 외 R² 1.69%(r1), 2.53%(r1+r12). 첫 30분 변동성이 높은 날 R² 3.3%, 낮은 날 0.6% — [WP](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf)
  - 타이밍 전략(마지막 30분만 보유): 연 6.67%, 표준편차 6.19%, Sharpe 1.08. 항상 매수는 연 −1.11%. **적중률 54.37% (항상 매수 50.42%)** — [WP](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf)
  - 불안정성: 2007–2009 R² 3.7%, 위기 기간을 빼면 0.7% — [WP](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf)
  - 비용: 저자들은 decimalization 뒤 비용이 낮아 비용 반영 후에도 CE 이득이 유의하다고 주장한다 — [WP](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf)
- **Baltussen, Da, Lammers, Martens (2021)**, JFE 142:377–403 ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365), [PDF](https://www3.nd.edu/~zda/intramom.pdf)): 주식, 채권, 원자재, 통화의 선물 60개 이상(1974–2020). 마지막 30분 수익률은 하루 나머지 수익률로 예측되고, 이후 며칠 동안 되돌아간다. 옵션 마켓메이커와 레버리지 ETF의 gamma hedging 수요가 원인이며, 지수의 hedging 수요가 클수록 효과도 크다. 레버리지·인버스 ETF가 기초지수보다 성과가 나쁠 수 있다고 지적한다 — [EUR](https://pure.eur.nl/en/publications/hedging-demand-and-market-intraday-momentum/), [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365)
- **Zarattini, Aziz, Barbon (2024, 2025-02 개정)** "Beat the Market" ([SSRN 4824172](https://papers.ssrn.com/abstract=4824172), [SFI RP 24-97](https://ideas.repec.org/p/chf/rpseri/rp2497.html)): 마지막 30분만 매매하지 않고, 가격이 "noise area"를 벗어나면 그 방향으로 진입한다. Noise area = 시가 ± 지난 14일 같은 시각의 평균 \|시가 대비 변동\| (배수 1). 동적 trailing stop을 쓴다. 2007–2024 초, 수수료와 slippage 반영, 총 1,985%, 연 19.6%, Sharpe 1.33. Dealer gamma imbalance별 성과도 분석했다 — [Concretum](https://concretumgroup.com/beat-the-market-an-effective-intraday-momentum-strategy-for-sp500-etf-spy/), [SFI](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy)
  - 진입 점검 주기(30분 단위), VWAP 기반 stop, 변동성 기반 sizing, gap 보정은 **[미확인]**: 기억에 따른 것이고 원문에서 확인하지 못했다.
- **Li, Sakkas, Urquhart (2022)**, JFM 57:100619 ([RePEc](https://ideas.repec.org/a/eee/finmar/v57y2022ics138641812100001x.html), [accepted PDF](https://centaur.reading.ac.uk/95566/1/Accepted-Version.pdf)): 16개 선진시장의 고빈도 자료. 대부분 국가에서 표본 내외 모두 유의하고, 16개 중 12개에서 유의하다 — [김찬수 외 2022 인용](https://doi.org/10.5762/KAIS.2022.23.7.344). 유동성이 낮고, 변동성이 높고, 정보가 이산적으로 들어올 때 강하다.
- **Limkriangkrai, Chai, Zheng (2023)** "Market intraday momentum: APAC evidence", Pacific-Basin Finance Journal 80:102086 ([RePEc](https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html)): 중국, 홍콩, 일본, 싱가포르, 한국의 ETF. 효과는 주로 **중국과 일본**에서 보인다. **한국은 약한 증거(marginal)**이고 홍콩과 싱가포르는 없다. 거래량보다 변동성의 영향이 크고, COVID 기간에는 약하다 — [ScienceDirect 요약](https://www.sciencedirect.com/science/article/pii/S0927538X2300152X) (본문 접근 403, 검색 요약 기준)
- 중국: Jin, Kearney, Li, Yang, J. Futures Markets ([DOI](https://doi.org/10.1002/fut.22084), [Hull](https://hull-repository.worktribe.com/output/3295550/intraday-time-series-momentum-evidence-from-china)). 중국 원자재 선물 4개에서 첫 30분이 마지막 30분을 예측한다. 야간 거래가 생긴 뒤에는 주간 첫 30분의 예측력이 사라지고, 야간 첫 30분이 새 예측변수가 됐다는 관련 연구도 있다 — [검색 요약](https://hull-repository.worktribe.com/output/3295550) **[미확인]**
- 대만: 대만 시장에서도 첫 30분이 마지막 30분을 양(+)으로 예측한다는 연구가 있다 — [airiti](https://www.airitilibrary.com/Article/Detail/U0001-2706202220180000) **[미확인, 학위논문으로 보임]**
- 옵션 gamma 조건: VIX 선물에서는 VIX 옵션 net gamma가 음일 때만 패턴이 나타난다 — [JBF 2022](https://www.sciencedirect.com/science/article/abs/pii/S0378426622003260) (저자 **[미확인]**). 개별주식 옵션 hedging 수요도 intraday momentum을 키운다 — [Park & Zhao 2025 WP](https://portal.northernfinanceassociation.org/viewp.php?n=2240183764). S&P 0DTE의 hedging 수요는 오히려 약한 intraday momentum을 예측한다는 연구도 있다 — [Adams et al. PDF](https://www.jean-sebastienfontaine.com/papers/0dte-options-volatility.pdf) **[미확인, 검색 요약만 봄]**
- 0DTE 시대 재검증: SPX 2022–2026 세션 1,085개에서 무조건부 마지막 30분 효과는 유의하지 않고, short-gamma 날에만 유의했다 — [DEV 블로그](https://dev.to/firmtape/intraday-momentum-is-dead-in-the-0dte-era-we-measured-it-on-1085-spx-sessions-43g0) **[비동료심사]**

### Inferences
- 적중률 기준으로 보면 미국 원 논문도 약 54%다. 한국에서 day-level 방향 kind를 만들 때는 52~55%를 현실적인 상한으로 보는 편이 맞다.
- 효과는 gamma와 변동성 regime에 따라 달라진다. 한국은 KOSPI200 옵션(위클리 포함)과 레버리지·인버스 ETF 시장이 크므로 메커니즘은 있을 수 있다. 다만 APAC 연구의 한국 결과가 약하므로 조건부(고변동성, short-gamma 추정)로 쓰는 편이 낫다.
- 2026-10 중순 KOSPI가 하루 −7.6% 같은 극단적 변동을 보였다는 보도가 있다 — [서울경제](https://en.sedaily.com/finance/2026/10/17/forced-stock-sales-top-80-billion-won-as-kospi-plunges-76). Gao 결과대로라면 이런 고변동성 구간에 intraday momentum이 강하다. 다만 표본이 짧아 검증은 어렵다.

### Gaps
- KOSPI 지수나 KOSPI200 선물만을 대상으로 Gao 방식 검증을 한 동료심사 논문은 찾지 못했다. APAC 연구는 ETF 기준이고, 한국 표본 기간과 ETF 종목은 본문을 못 봐서 **[미확인]**이다.
- Baltussen et al.의 60개 선물에 KOSPI200 선물이 들어 있는지 확인하지 못했다.
- Zarattini noise-area 전략의 독립 학술 재현은 찾지 못했다.

---

## 2. Opening Range Breakout (ORB)

### Takeaway
ORB를 지지하는 최근 증거는 대부분 같은 업계 저자 그룹(Zarattini, Aziz, Barbon, 2023–2024 SSRN)에서 나왔다. 레버리지 ETF와 "stocks in play" 선별에 크게 기댄다. 지수 ORB는 spread와 slippage를 넣은 재현에서 0과 구분되지 않았다는 비판이 있다.

### Cited Findings
- **Zarattini & Aziz (2023)** "Can Day Trading Really Be Profitable?" ([SSRN 4416622](https://papers.ssrn.com/abstract=4416622)): QQQ 2016–2023, 5분 ORB. 첫 5분이 상승이면 두 번째 5분 시작에 매수, 하락이면 매도. 시가와 첫 5분 종가가 거의 같으면 거래하지 않는다. 수수료 반영 연 alpha 33%, TQQQ 사용 시 1,484% (QQQ 169%). 브로커 레버리지 제약을 언급한다 — [Concretum](https://concretumgroup.com/can-day-trading-really-be-profitable/), [CXO](https://www.cxoadvisory.com/technical-trading/day-trading-with-an-opening-range-breakout-strategy)
- 비판적 재현: NQ 등 지수 5개에서 총수익은 재현됐다. 하지만 원 논문은 수수료만 넣고 spread와 slippage를 넣지 않았고, 표본 외 기간도 없다. 비용을 넣으면 5개 모두 0과 구분되지 않고 4개는 음수다 — [MQL5 블로그](https://www.mql5.com/en/blogs/post/776235) **[비동료심사]**
- **Zarattini, Barbon, Aziz (2024)** "A Profitable Day Trading Strategy For The U.S. Equity Market" ([SSRN 4729284](https://papers.ssrn.com/abstract=4729284), [SFI RP 24-98](https://ideas.repec.org/p/chf/rpseri/rp2498.html)): 미국 7,000여 종목 2016–2023. "Stocks in Play"(뉴스 등으로 평소보다 거래가 활발한 종목)로 제한하면 성과가 좋아진다. 상위 20종목 포트폴리오는 순수익 1,600% 이상, Sharpe 2.81, 연 alpha 36%다 (S&P500 198%). 15, 30, 60분 opening range도 비교했다 — [Barbon 페이지](https://abarbon.com/papers/a-profitable-day-trading-strategy-for-the-u-s-equity-market), [Concretum](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/)
  - 재현 선별 기준: 가격 $5 초과, 14일 평균 거래량 100만 주 이상, ATR $0.50 초과. 순위는 첫 5분 거래량 / 지난 14일 첫 5분 평균 거래량. QuantConnect 재현 Sharpe는 2.4, beta는 약 0이다 — [QuantConnect](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)
- 오래된 학술 증거: Holmberg, Lönnbark, Lundström (2013) "Assessing the profitability of intraday opening range breakout strategies", Finance Research Letters 10(1):27–33 — [RePEc](https://ideas.repec.org/a/eee/finlet/v10y2013i1p27-33.html). 결과 수치는 **[미확인]**이다.
- 한국 블로그 실측: 2026년 5개 시장 298세션 5분봉에서 개장 30분 방향과 하루 방향의 일치율은 72.5%였다. 그러나 첫 30분을 빼면 54.7%이고, KOSPI만 보면 44.1%였다 — [chartget](https://chartget.co.kr/market-open-first-30-minutes-backtest/) **[비동료심사, 표본 작음]**

### Inferences
- "개장 30분 방향 = 하루 방향" 일치율은 첫 30분 수익률이 하루 수익률에 포함돼 있어서 생기는 기계적 착시다. 라벨은 반드시 **첫 구간 이후의 수익률**로 정의해야 한다.
- 이 엔진은 주문을 내지 않으므로, ORB는 "opening range 상단과 하단 중 어느 쪽을 먼저 touch 하는가"라는 range/triple-barrier 라벨로 옮기기 쉽다.

### Gaps
- Crabel(1990) 원전, ORB의 아시아(한국) 동료심사 검증은 찾지 못했다.

---

## 3. Intraday periodicity와 end-of-day 효과: Heston, Korajczyk, Sadka (2010), Bogousslavsky (2016, 2021)

### Takeaway
같은 시간대 수익률은 며칠에 걸쳐 지속되고(institutional 분할·정기 리밸런싱), 직전 구간과는 반전한다. 장 마감 무렵 premium이나 mispricing 청산이 몰린다. **한국 KOSPI200에서도 장 마감 30분 periodicity(지속)와 09:00–09:10 반전이 확인됐다(2016–2021).**

### Cited Findings
- **Heston, Korajczyk, Sadka (2010)**, JF 65(4):1369–1407 ([DOI](https://doi.org/10.1111/j.1540-6261.2010.01573.x), [arXiv 1005.3535](https://arxiv.org/abs/1005.3535)): 하루를 30분 구간 13개로 나눈다. 13, 26, 39… lag(즉 다른 날 같은 시각)에서 양의 관계가 있고 40거래일 이상 지속된다. 첫 30분과 마지막 30분에서 더 강하다. 짧은 lag에서는 반전(bid-ask bounce, 1시간 미만의 일시적 유동성 불균형)이 나타난다. 거래량, order imbalance, 변동성, spread로는 설명되지 않는다 — [Kellogg](https://www.kellogg.northwestern.edu/academics-research/research/detail/2010/intraday-patterns-in-the-cross-section-of-stock-returns/?p=1)
  - 영국과 브라질에서는 재현됐고 중국은 약하다 — [Aston](https://research.aston.ac.uk/en/publications/investor-clientele-and-intraday-patterns-in-the-cross-section-of-/)
- **Bogousslavsky (2016)** "Infrequent Rebalancing, Return Autocorrelation, and Seasonality", JF 71(6):2967–3006 ([DOI](https://doi.org/10.1111/jofi.12436), [EPFL PDF](https://memento.epfl.ch/public/upload/files/Paper_Bogousslavsky.pdf)): 투자자가 가끔만 리밸런싱한다는 모형으로 장중과 일간 자기상관을 설명한다. 리밸런싱 주기에서 자기상관의 부호가 양으로 바뀌고, 이것이 seasonality를 만든다.
- **Bogousslavsky (2021)** "The cross-section of intraday and overnight returns", JFE 141(1):172–194 ([RePEc](https://ideas.repec.org/a/eee/jfinec/v141y2021i1p172-194.html), [WP PDF](https://finance.unibocconi.eu/sites/default/files/files/media/attachments/The%2520Cross-Section%2520of%2520Intraday%2520and%2520Overnight%2520Returns_21Dec1620170112151545.pdf)): overnight 보유에는 증거금, 대차비용, 위험이 있어 arbitrageur가 장 마감 전에 청산한다. 그래서 mispricing factor는 장중에 벌다가 장 마감에 손실을 본다. 표본 후반부에서 더 강하다. WP 버전에 따르면 size와 illiquidity premium은 마지막 30분에 실현된다.
- **Baltussen, Da, Soebhag (2025-04 WP)** "End-of-Day Reversal" ([PDF](https://academicweb.nd.edu/~zda/EOD.pdf)): 미국 TAQ 1993–2019. 개별 종목의 ROD3(overnight + 첫 30분 + 장 중간) 수익률이 마지막 30분 수익률을 **횡단면에서 음으로** 예측한다(t > 10). 마지막 직전 30분은 건너뛰어 bid-ask bounce를 배제했다. L-S는 하루 3.78bp(VW, 연 9.5%)에서 6.86bp(EW, 연 17.3%)이고 총수익 기준이다. 대부분 intraday loser 쪽의 양의 price pressure에서 나온다. 유동성이나 gamma hedging으로는 설명되지 않는다. 원인은 주목을 받은 retail 매수와 short-seller의 장 마감 위험관리다. 저자 스스로 비용 뒤에는 많은 투자자가 이용하기 어려울 수 있다고 쓴다.
- **한국: 김찬수·최흥식·김선웅 (2022)** "주식 수익률의 일 중 주기성 연구", 한국산학기술학회논문지 23(7):344–352 ([DOI](https://doi.org/10.5762/KAIS.2022.23.7.344), [PDF](https://www.kais99.org/jkais/journal/Vol23No07/vol23no07p41.pdf)): KOSPI200 전 종목, 10분 수익률, 2016-08-01~2021-12-30, 구간 39개.
  - 09:00–09:10 수익률은 **전일 같은 구간과 음의 관계**다. 대형주와 개인 비중이 높은 종목에서 강하다(전일 하위 10% 매수, 상위 10% 매도). 대형주 L-S −0.1215%(t=−7.19)는 반대 방향으로 하면 +라는 뜻이다.
  - 15:00–15:30(구간 37~39)은 **전일 같은 구간과 양의 관계**다. 예: 대형주 15:20–15:30 L-S +0.061%(t=11.8). 개인 비중이 낮은 종목에서 더 강하다.
  - 구간별 평균 10분 수익률: 09:00–09:10 −0.0125%, 15:10–15:20 **+0.0363%**, 15:20–15:30(종가 단일가) **−0.0267%**.
  - 비용은 반영하지 않았다. 저자들은 거래세가 없는 주식선물로의 확장을 제안한다.
- 이전 한국 연구(위 논문의 문헌 정리에서 재인용, 원문 **[미확인]**): Kim & Lee (2012, POSRI Business Review 12(3):124–152)는 KOSPI200 2010년 6개월 30분 수익률에서 하루 lag 양의 관계와 폐장 효과를 보였다. Jang (1992)은 점심 휴장이 있던 시절 20분 수익률에서 V자(정오 휴장 때 급락) 패턴을 보였다 **[오래됨]**. Gang (2001)은 KOSPI200 선물 수익률에 개장·폐장 규칙성은 없고 변동성만 개장과 폐장에 높다고 했다 **[오래됨]**. Lee (2013)는 가격발견 기여도가 개장 직후 가장 높은 역J자라고 했다 — [김찬수 외 2022](https://www.kais99.org/jkais/journal/Vol23No07/vol23no07p41.pdf)

### Inferences
- 한국에서 **15:10–15:20 양, 15:20–15:30 단일가 음**이라는 평균 패턴은 "장 마감 직전 연속매매 구간"과 "종가 단일가"를 다른 라벨로 다뤄야 한다는 뜻이다.
- 시장 수준 momentum(Gao, Baltussen)과 종목 횡단면 reversal(EOD)은 방향이 반대다. 세 종목의 마지막 30분은 "시장 성분(beta × KOSPI200 선물)"과 "고유 성분"으로 나눠 예측하는 편이 일관된다.

### Gaps
- 김찬수 외(2022)는 횡단면 L-S이고 거래세와 spread를 반영하지 않았다. 세 종목 단일 시계열에도 성립하는지는 알 수 없다.
- 미국 EOD reversal의 한국 재현은 찾지 못했다.

---

## 4. Overnight 대 intraday: Lou, Polk, Skouras (2019), Kelly & Clark, gap fade 대 gap go

### Takeaway
미국 주식의 수익은 대부분 overnight에 생기고, 장중에는 0이거나 음이다. Momentum은 overnight, 다른 anomaly는 장중에 실현된다(tug of war). **한국도 overnight와 장중 수익률은 음의 관계(평균적으로 gap fade)**이고, 개인은 overnight 움직임에 역행하며 기관과 외국인은 순행한다.

### Cited Findings
- **Lou, Polk, Skouras (2019)** "A tug of war: Overnight versus intraday expected returns", JFE 134:192–213 ([LSE](https://researchonline.lse.ac.uk/id/eprint/87481), [PDF](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf)): 이익은 overnight에만(reversal, 여러 momentum) 또는 장중에만 생기고, 두 구간의 부호는 보통 반대다. 기관은 장중에 momentum과 반대로 거래하고, 개인은 overnight 쪽이다. 대형주와 고가주에서 더 강하다.
- **Kelly & Clark (2011)**, Journal of Asset Management 12(2):132–145 ([RePEc](https://ideas.repec.org/a/pal/assmgt/v12y2011i2d10.1057_jam.2011.2.html)): QQQQ 1999–2006에서 close→open premium은 평균 약 +23.7%, open→close는 약 −23.3%다 (연율 여부 **[미확인]**) — [Lafayette](https://dspace.lafayette.edu/handle/10385/1012) **[오래됨]**
- **Berkman, Koch, Tuttle, Zhang (2012)** "Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open", JFQA 47(4):715–741 ([RePEc](https://ideas.repec.org/a/cup/jfinqa/v47y2012i04p715-741_00.html)): overnight는 양, 장중은 반전한다. 시가가 장중 가격보다 높게 형성되기 때문이다. 최근 retail 주목을 받은 종목, 가치평가가 어렵고 차익거래 비용이 큰 종목, retail sentiment가 높은 시기에 강하다. 시가 매수의 암묵 비용이 effective half spread를 넘는 경우가 많다.
- **Akbas, Boehmer, Jiang, Koch (2022)** "Overnight returns, daytime reversals, and future stock returns", JFE 145(3):850–875 ([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0304405X21004116)). overnight 뒤 장중 반전의 빈도가 미래 수익률을 예측한다. 방향과 크기는 **[미확인]**이다.
- **한국: Ham, Ryu, Webb, Yu (2023)** "How do investors react to overnight returns? Evidence from Korea", Finance Research Letters 54:103779 ([RePEc](https://ideas.repec.org/a/eee/finlet/v54y2023ics1544612323001526.html), [XJTLU](https://scholar.xjtlu.edu.cn/en/publications/how-do-investors-react-to-overnight-returns-evidence-from-korea)): 한국 주식의 overnight와 장중 수익률은 **음의 관계**다. 개인은 overnight 수익률에 역행하고, 국내 기관과 외국인은 positive feedback으로 거래한다. overnight 상승은 good news일 때만 같은 방향 장중 움직임으로 이어진다 — [검색 요약](https://www.sciencedirect.com/science/article/abs/pii/S1544612323001526)
- **한국: Kongahawatte (2025)** "Overnight versus Intraday Momentum and Investor Heterogeneity: Evidence from the Korean Equity Market", Asian Finance Research ([저널](https://journals.sjp.ac.lk/index.php/afr/article/view/7881), 접근 시 404): 2014–2017 투자자 유형별 거래로 overnight와 장중 momentum, 그리고 서로 상쇄되는 구간 간 reversal을 설명한다. overnight는 retail의 주목 거래, 장중은 국내 기관의 차익거래가 원인이다 — 검색 초록 기준 **[미확인]**
- **한국 ETF: 이상기·홍정훈 (2022)** "Intraday Return Reversals: Empirical Evidence from the Korean ETF Market", 금융정보연구 11(1):55–77 ([DOI](https://doi.org/10.35214/rfis.11.1.202202.003), [SSU](https://ssu.scholarworks.kr/handle/2018.sw.ssu/42155)): 한국 ETF의 overnight 수익률은 유의하게 양이고, 장중 수익률은 음이다. attention 가설만으로는 부족하고, 공매도 제약 아래 의견 불일치가 유의한 요인이다.
- 한국 day trader의 overnight 재고: 외국인 day trader의 overnight 재고는 다음 주가 상승을 예측하고, 국내 개인은 손실 포지션을 유지하는 경향이 있다 — [Park & Park, SSRN 2633397](https://www.ssrn.com/abstract=2633397) **[오래됨, 2006 자료]**
- Gap fade 근거: S&P500과 항셍 지수선물에서 큰 개장 움직임이 크기에 비례해 부분 반전한다. 비용 뒤 소폭 이익이 남는다 — [Fung, Mok, Lam 2000, JBF 24(7):1179–1201](https://scholars.hkbu.edu.hk/en/publications/intraday-price-reversals-for-index-futures-in-the-us-and-hong-kon/) **[오래됨]**. 15년 미국 재검증에서는 비용이 이익을 크게 줄였다 — [Grant, Wolf, Yu 2005, JBF 29(5)](https://ideas.repec.org/a/eee/jbfina/v29y2005i5p1311-1327.html)
- KOSPI200 야간선물(Eurex 연계)의 가격발견을 이용한 시간외 매매 수익성 연구가 있다 — [이우백 2015 학술발표](https://www.earticle.net/Article/A249109). 결과 수치는 **[미확인]**이다.

### Inferences
- 한국 평균은 gap fade 쪽이다. Gap go(ORB)는 뉴스나 거래량이 동반된 "stocks in play" 날에 조건부로만 기대하는 편이 근거와 맞다.
- 개인이 overnight에 역행하고 기관과 외국인은 순행한다는 결과(Ham et al.)는, 엔진이 가진 투자자별 수급으로 gap 이후 방향을 조건화할 수 있다는 뜻이다.

### Gaps
- 한국의 overnight 대 장중 분리를 KOSPI200 대형주(삼성전자, SK하이닉스)만으로 최근(2023–2026) 표본에서 본 연구는 찾지 못했다. NXT(2025~) 출범 뒤 overnight 정의(15:30 종가 대 20:00 NXT 종가 대 08:00 NXT 시가)가 바뀐 효과를 다룬 연구도 찾지 못했다.

---

## 5. VWAP reversion, 큰 움직임 뒤 장중 반전, 장 마감 전과 종가 단일가, 점심시간

### Takeaway
**VWAP z-score reversion을 직접 검증한 동료심사 논문은 찾지 못했다.** 장중 반전 증거는 (a) 1시간 미만의 lag(HKS), (b) 개장 직후 큰 움직임(Fung et al.), (c) 개별 종목의 마지막 30분(EOD)에 국한된다. 종가 단일가 가격은 overnight에 되돌아가는 경향이 있다(미국). 한국은 점심 휴장이 없어 lunch 효과 연구의 의미가 작다.

### Cited Findings
- 짧은 lag 반전은 bid-ask bounce와 1시간 미만의 일시적 유동성 불균형에서 나온다 — [HKS 2010](https://arxiv.org/abs/1005.3535)
- 한국 09:00–09:10 반전(전일 같은 구간 대비)은 개인 비중이 높은 종목에서 강하다 — [김찬수 외 2022](https://doi.org/10.5762/KAIS.2022.23.7.344)
- 종가 auction: 미국 closing auction이 일 거래량에서 차지하는 비중은 2010년 3.1%에서 2018년 7.5%로 늘었다. 지수 리밸런싱과 월말에 몰리고 ETF·인덱스 거래와 관련된다. 종가와 종가 호가 중간값의 괴리는 overnight에 대부분 되돌아간다(대형주는 완전히). auction 자체의 가격발견은 작다 — [Bogousslavsky & Muravyev 2023, JFM 66:100852](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html)
- 한국 종가·시가 단일가:
  - KRX 임의종료(Random End)는 가격발견과 안정화에 비교적 효과적이다. 다만 발동 직전 overshooting이 있다. 발동률은 KOSPI 약 0.8%, KOSDAQ 약 2.1%이고 변동성이 크고 소형·저가인 종목에서 잦다 — [재무연구 28(4), 2015](http://www.korfin.org/korfin_file/forum/28-4-02.pdf) (저자 **[미확인]**)
  - 장 마감 동시호가(10분) 종가 시세조종 사례에서 비정상 상승은 KOSPI 0.41~0.82%, KOSDAQ 0.55~0.86%였다 — [KCI](https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART001743825)
  - 2019년 시가 단일가 호가 접수 시간을 30분으로 줄인 뒤에도 가격발견은 이전 후반 30분과 최소 같은 수준이었다 — [박영석·안일찬 2023, 한국증권학회지 52(2):213–237](https://www.e-kjfs.org/journal/view.php?number=966)
  - 정규장 종료가 15:00에서 15:30으로 바뀐 것은 2016-08-01이다 — [김찬수 외 2022](https://www.kais99.org/jkais/journal/Vol23No07/vol23no07p41.pdf)
- 점심시간: 한국은 과거 정오 휴장 시절 V자 패턴이 보고됐다(Jang 1992, 재인용) **[오래됨]**. Yoon et al.(1994)은 지수 1분 수익률에서 U자를 보였다 — [김찬수 외 2022 문헌 정리](https://www.kais99.org/jkais/journal/Vol23No07/vol23no07p41.pdf)

### Inferences
- 엔진의 Gauss(VWAP z-score reversion)가 2026-09~10 자료에서 reversion을 배우지 못한 것은 문헌과 맞는다. 장중 반전은 특정 시간대(개장 10분, 개별 종목 마지막 30분)와 극단적 움직임에서만 기대된다. Gauss는 시간대 조건부로 바꾸거나 극단 z에서만 켜는 편이 근거에 맞다.

### Gaps
- VWAP reversion 학술 증거, 한국의 lunch-hour 효과(현재 연속 매매), 한국 종가 단일가 가격의 다음 날(또는 NXT 시간외) 되돌림을 다룬 최근 연구는 찾지 못했다. 한국 점심 휴장 폐지 시점(2000년으로 기억)도 이번에 확인하지 못했다.

---

## 6. 기술적 매매 규칙: 무엇이 data-snooping과 비용 보정 뒤에 남는가

### Takeaway
1990년대 이전 일봉 규칙(BLL)은 강해 보였지만, Reality Check, SPA, FDR로 보정하고 비용을 넣으면 주식시장에서는 대부분 사라진다. 장중(5분) 규칙도 마찬가지다. FX와 신흥시장에서는 일부 남는다는 결과(Hsu, Taylor, Wang)와 남지 않는다는 결과가 맞선다. 호가 불균형은 **예측력은 있지만 매매 이익으로 이어지지 않는다**(Tokyo).

### Cited Findings
- **Brock, Lakonishok, LeBaron (1992)**, JF 47(5):1731–1764 ([DOI](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x), [RePEc](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html)): DJIA 일봉에서 MA와 trading-range-break 규칙 26개의 매수 신호 뒤 수익률이 더 높다 **[오래됨, 비용 미반영]**.
- **Lo, Mamaysky, Wang (2000)** "Foundations of Technical Analysis", JF 55(4):1705–1765 ([DOI](https://doi.org/10.1111/0022-1082.00265), [NBER w7613](https://www.nber.org/papers/w7613)): kernel regression으로 차트 패턴을 자동 인식한다(미국 1962–1996). 일부 지표가 증분 정보를 주고 Nasdaq에서 두드러지지만, 강한 수익성 주장은 아니다 **[오래됨]**.
- **Sullivan, Timmermann, White (1999)**, JF 54(5):1647–1691 ([RePEc](https://ideas.repec.org/p/ehl/lserod/119144.html)): BLL의 규칙 우주를 넓히고 White's Reality Check bootstrap으로 data-snooping을 보정했다(DJIA 100년).
- **Park & Irwin (2007)**, J. Economic Surveys 21(4):786–826 ([RePEc](https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html)): 현대 연구 95개 중 56개는 긍정, 20개는 부정, 19개는 혼재다. 초기 연구에서는 FX와 선물에서만 이익이 났고 주식은 아니었다. 현대 연구에서는 1990년대 초까지 이익이 났다. data snooping, 사후 규칙 선택, 위험과 비용 추정 문제를 지적한다.
- **Bajgrowicz & Scaillet (2012)** "Technical trading revisited", JFE 106(3):473–491 ([RePEc](https://ideas.repec.org/a/eee/jfinec/v106y2012i3p473-491.html)): DJIA 1897–2011, FDR 보정. 미래의 최고 규칙을 사전에 고를 수 없고, **낮은 거래비용만으로 표본 내 성과도 완전히 상쇄된다**.
- **Hsu, Taylor, Wang (2016)** "Technical trading: Is it still beating the foreign exchange market?", J. International Economics 102:188–208 ([WUSTL](https://profiles.wustl.edu/en/publications/technical-trading-is-it-still-beating-the-foreign-exchange-market/)): 30개 통화, 약 45년, 규칙 21,195개, stepwise SPA. 선진과 신흥 통화 모두에서 예측력과 초과이익이 있고 표본 외에서도 유지된다. 비용은 편도 0.045%(선진), 0.21%(신흥)를 가정했다. 반면 Hsu & Taylor (2013)와 Coakley et al. (2016)은 비용 뒤 증거가 거의 없다고 했다 — [검색 요약](https://repository.essex.ac.uk/18805/1/1-s2.0-S1057521916301958-main.pdf) **[미확인, 2차 인용]**
- **Marshall, Cahan, Cahan (2008)** "Does intraday technical analysis in the U.S. equity market have value?", J. Empirical Finance 15(2):199–210 ([RePEc](https://ideas.repec.org/a/eee/empfin/v15y2008i2p199-210.html)): SPY 5분 2002–2003, 규칙 7,846개(filter, MA, S/R, channel breakout, OBV). 보정 전에는 1% 유의 42개, 5% 유의 163개이고 channel breakout이 가장 낫다 — [CXO 요약](https://cxoadvisory.com/technical-trading/technical-analysis-anathema-to-the-academic-world). data-snooping 보정 뒤 결론은 **[미확인]**.
- **Yamamoto (2012)** "Intraday technical analysis of individual stocks on the Tokyo Stock Exchange", JBF 36(11):3033–3047 ([RePEc](https://ideas.repec.org/a/eee/jbfina/v36y2012i11p3033-3047.html), [NCCU](https://nccur.lib.nccu.edu.tw/handle/140.119/67028)): Nikkei225 종목에서 order-flow imbalance와 order-book imbalance가 절반 넘는 종목의 단기 수익률을 예측한다. 그래도 limit order 전략을 포함해 **어떤 전략도 buy-and-hold를 넘지 못했다**.

### Inferences
- krx-quant는 Newton, Gauss, Kalman, OFI, Hawkes, HMM, LightGBM 등 여러 모델과 여러 라벨(k, horizon)을 동시에 시험한다. 그러면 사실상 다중검정이 된다. 새 day-level kind를 추가할 때는 SPA, Reality Check, FDR 계열 보정이나 표본 외 고정 검증이 필요하다.
- Yamamoto 결과는 "적중률(예측력)이 있다"와 "비용 뒤 이익이 난다"가 다르다는 점을 보여 준다. 사용자가 적중률로 모델을 평가하는 것은 연구 기준으로는 맞지만, 인버스 ETF 매매로 옮길 때는 비용과 괴리를 따로 봐야 한다.

### Gaps
- STW(1999)의 표본 외(1987~) 결과, Marshall et al.(2008)의 보정 뒤 결론은 원문을 확인하지 못했다.
- 한국 시장의 장중 기술적 규칙에 data-snooping 보정을 적용한 연구는 이번에 찾지 못했다.

---

## 7. 한국(KRX/KOSPI) 특화 증거 요약

### Takeaway
한국 장중 증거는 (1) 시장 수준 intraday momentum이 약하다(APAC ETF), (2) 같은 시간대 periodicity가 있다(장 마감 지속, 개장 10분 반전, KOSPI200 2016–2021), (3) overnight와 장중이 음의 관계이고 개인과 기관이 반대로 반응한다, (4) 시가와 종가 단일가 제도(임의종료, 시가 접수 30분)의 가격발견 연구로 요약된다. **KOSPI200 선물의 Gao 방식 검증, 08:30–09:00 시가 단일가 예상체결가의 방향 예측력, 15:20–15:30 종가 단일가 움직임의 다음 날 되돌림을 직접 다룬 최근 동료심사 연구는 찾지 못했다.**

### Cited Findings
- 한국 ETF의 intraday momentum은 약하다 — [Limkriangkrai et al. 2023](https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html)
- KOSPI200 10분 periodicity와 평균 패턴(15:10–15:20 +0.0363%, 15:20–15:30 −0.0267%) — [김찬수 외 2022](https://doi.org/10.5762/KAIS.2022.23.7.344)
- overnight와 장중의 음의 관계, 투자자 유형별 반응 — [Ham et al. 2023](https://ideas.repec.org/a/eee/finlet/v54y2023ics1544612323001526.html); ETF — [이상기·홍정훈 2022](https://doi.org/10.35214/rfis.11.1.202202.003); 2014–2017 investor heterogeneity — [Kongahawatte 2025](https://journals.sjp.ac.lk/index.php/afr/article/view/7881) **[미확인]**
- 시가 단일가 30분 단축 효과 — [박영석·안일찬 2023](https://www.e-kjfs.org/journal/view.php?number=966); 임의종료 — [재무연구 2015](http://www.korfin.org/korfin_file/forum/28-4-02.pdf)
- 한국 단기반전과 단기모멘텀(일·주 단위로 보임) — [KCI ART002987582](https://www.kci.go.kr/kciportal/landing/article.kci?arti_id=ART002987582) (내용 **[미확인]**)

### Inferences
- 한국에서는 "시장 momentum형 마지막 30분"보다 **"같은 시간대 periodicity"와 "overnight 뒤 fade"**가 증거가 더 직접적이다.

### Gaps
- NXT(08:00–20:00) 출범 뒤의 장중 패턴 연구가 없다. 2025년 이후라서 아직 나오지 않은 것으로 보인다.
- 한국 증권거래세율(2026년)은 이번에 확인하지 않았다. 비용 계산 전에 확인이 필요하다.

---

## 8. 최근(2023–2026) 연구 흐름

### Takeaway
(1) intraday momentum의 원인을 옵션·0DTE gamma와 레버리지 ETF hedging으로 보는 연구, (2) 개별 종목 end-of-day reversal, (3) 업계 저자의 ORB와 noise-area 전략, (4) APAC과 한국 overnight 연구가 주류다. 0DTE 확산 뒤 미국 무조건부 효과가 약해졌다는 주장이 있으나 동료심사 확인은 없다.

### Cited Findings
- End-of-Day Reversal (2025 WP) — [PDF](https://academicweb.nd.edu/~zda/EOD.pdf)
- Inelastic Hedging Demand and Intraday Momentum (2025 WP) — [NFA](https://portal.northernfinanceassociation.org/viewp.php?n=2240183764)
- 0DTE hedging과 intraday momentum — [Adams et al.](https://www.jean-sebastienfontaine.com/papers/0dte-options-volatility.pdf) **[미확인]**; 0DTE 거래와 gamma 위험 — [ResearchGate](https://www.researchgate.net/publication/377984312_0DTEs_Trading_Gamma_Risk_and_Volatility_Propagation) **[미확인]**
- Beat the Market (2024/2025), ORB stocks in play (2024) — [SSRN 4824172](https://papers.ssrn.com/abstract=4824172), [SSRN 4729284](https://papers.ssrn.com/abstract=4729284)
- APAC intraday momentum (2023), 한국 overnight (2023, 2025) — 위 4, 7절

### Inferences
- 한국에서 대응 변수는 KOSPI200 옵션(위클리 포함) dealer gamma 추정과 레버리지·인버스 ETF 일일 리밸런싱 규모다. 엔진은 옵션 IV는 갖고 있지만 미결제약정 기반 gamma나 ETF AUM은 없을 수 있다.

### Gaps
- 2026년에 발표된 동료심사 intraday 전략 논문은 이번 검색에서 특정하지 못했다.

---

## 9. krx-quant 적용 메모

### Takeaway
엔진이 이미 가진 1분봉(1년), 체결과 10호가, KOSPI200 선물, 투자자 수급, 옵션 IV, NXT 시세로 만들 수 있는 day-level 예측 kind 후보는 아래와 같다. 근거가 강한 순서다. 모두 "신호만 내고 주문하지 않는다"는 원칙에 맞게 **방향(up/flat/down) 또는 touch 라벨**로 정의한다.

### Cited Findings (후보별 근거)
1. **`last30` 방향 kind (15:00→15:20 연속매매 마감 또는 →15:30 종가)**: 입력은 overnight + 09:00–15:00 수익률(ROD), 첫 30분 수익률, KOSPI200 선물 ROD, 당일 실현변동성. 근거는 [Gao et al.](https://papers.ssrn.com/abstract=2440866), [Baltussen et al.](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365)이다. 한국에서 약하다는 [APAC](https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html) 결과가 있으므로 고변동성 날 조건부로 평가한다.
2. **같은 시간대 periodicity feature와 kind**: 전일(그리고 지난 n일) 같은 10분 구간 수익률을 feature로 넣는다. 15:00–15:30은 지속, 09:00–09:10은 반전이다. 4분 모델에도 바로 넣을 수 있다 — [김찬수 외 2022](https://doi.org/10.5762/KAIS.2022.23.7.344), [HKS](https://arxiv.org/abs/1005.3535)
3. **`open_to_close` 방향 kind (09:05 또는 09:30 시점 예측)**: 입력은 overnight gap(전일 KRX 종가 → 시가), NXT 프리마켓(08:00–08:50) 수익률, 미국 반도체 지수 overnight, KOSPI200 야간선물, 전일 투자자별 수급. 기본 가설은 gap fade다 — [Ham et al.](https://ideas.repec.org/a/eee/finlet/v54y2023ics1544612323001526.html), [Berkman et al.](https://ideas.repec.org/a/cup/jfinqa/v47y2012i04p715-741_00.html)
4. **ORB touch kind**: 첫 5, 15, 30분 range를 잡고 장 마감 전에 상단과 하단 중 어느 쪽을 먼저 touch 하는지 예측한다. 기존 range와 triple-barrier kind의 확장이다. gating은 "첫 5분 거래량 / 14일 평균 첫 5분 거래량"(stocks-in-play 지표)이다 — [Zarattini et al.](https://papers.ssrn.com/abstract=4729284)
5. **Noise-area 이탈 kind**: 시각별 경계 = 시가 × (1 ± 지난 14일 같은 시각의 평균 |시가 대비 변동|). 경계를 넘은 뒤 장 마감까지 그 방향이 유지되는지 예측한다. 1년 치 1분봉으로 바로 계산할 수 있다 — [Zarattini, Aziz, Barbon](https://papers.ssrn.com/abstract=4824172)
6. **종가 단일가 kind (`auction_move`)와 `close_to_NXT`**: 15:20 시점 가격 대비 종가 방향, 그리고 종가 대비 NXT 시간외(또는 다음 날 시가) 되돌림이다. 미국에서는 종가 괴리가 overnight에 되돌아간다 — [Bogousslavsky & Muravyev](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html). 한국 평균 15:20–15:30 수익률은 음이다 — [김찬수 외 2022](https://doi.org/10.5762/KAIS.2022.23.7.344)

### Inferences
- **적중률 기대치**: 미국 원 논문의 마지막 30분 타이밍 적중률이 54.37%(기준 50.42%)다. day-level kind의 현실적 목표는 52~55%다.
- **통계적 검정력 문제**(추론): 하루에 라벨이 1개인 kind는 1년이 약 245개 표본이다. 적중률 표준오차가 약 √(0.25/245) ≈ 3.2%p이므로 54%와 50%를 95%로 구분하기 어렵다. 세 종목과 KOSPI200 선물을 함께 쓰고, 더 긴 일·분 자료를 확보하고, 사전에 정한 표본 외 기간으로 검증해야 한다.
- **다중검정**: 여러 kind × k × horizon × 모델을 시험하므로 SPA, Reality Check, FDR 보정이 필요하다 — [Bajgrowicz & Scaillet](https://ideas.repec.org/a/eee/jfinec/v106y2012i3p473-491.html), [Hsu, Taylor, Wang](https://profiles.wustl.edu/en/publications/technical-trading-is-it-still-beating-the-foreign-exchange-market/)
- **시장 대 종목 분해**: 세 종목의 마지막 30분 = beta × 선물(momentum 가설) + 고유 성분(EOD reversal 가설)으로 나눠 예측한다. 두 문헌이 반대 방향이라서 섞으면 서로 상쇄될 수 있다.
- **인버스 ETF 주의**: down 신호를 인버스·레버리지 ETF로 옮기면, 그 ETF들의 장 마감 리밸런싱이 바로 intraday momentum의 원인이고 이후 되돌아간다 — [Baltussen et al.](https://pure.eur.nl/en/publications/hedging-demand-and-market-intraday-momentum/). 마지막 30분 신호를 인버스 ETF로 받으면 종가 근처에서 가장 불리하게 체결될 수 있다.
- **Gauss 재설계 단서**: 무조건부 VWAP reversion보다 "09:00–09:10 반전"과 "종목 고유 성분의 마지막 30분 reversal"처럼 시간대 조건부 reversion이 근거와 맞다.

### Gaps
- 엔진에 KOSPI200 옵션 미결제약정(dealer gamma 추정용)과 레버리지·인버스 ETF AUM 자료가 있는지 확인이 필요하다. 이 자료가 없으면 Baltussen식 조건화는 IV와 실현변동성으로 대신해야 한다.
- NXT 시간대를 포함하면 "overnight"와 "마지막 30분"의 정의가 KRX 단독 연구와 달라진다. 이를 다룬 선행 연구는 없다.
