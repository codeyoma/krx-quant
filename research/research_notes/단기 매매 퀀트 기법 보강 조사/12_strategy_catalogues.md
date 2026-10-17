# 전략 카탈로그로 본 단기 전략 분류와 1차 조사에서 빠진 계열

*기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다.*

조사 방법과 도구 상태:
- 1차 보고서(`research/reports/단기 매매 퀀트 기법 조사.md`)와 1차 노트 10편을 먼저 읽고 무엇을 다뤘는지 확인했다.
- WebSearch는 10회 썼고 끝까지 정상 작동했다.
- Wikipedia는 원문 wikitext를 직접 받아 절 제목과 목록을 읽었다.
- Quantpedia는 curl을 막는다(HTTP 466 "Access Forbidden"). WebFetch로는 무료 페이지만 열린다. 유료 페이지(예: `/strategies/overnight-anomaly/`, `/strategies/lunar-cycle-in-equity-market`)는 홈페이지로 넘어간다.
- "151 Trading Strategies" 영어판 PDF는 받지 못했다. arXiv 1912.04492는 같은 책의 **스페인어판**이고, 전략 이름은 영어 병기다. 이 판의 목차와 일부 본문으로 대조했다.
- 논문 서지는 Crossref API로 제목, 저자, 연도, DOI를 확인했다. 초록까지 읽은 것은 따로 표시했다.
- 카탈로그의 성과 수치는 대부분 원 논문의 표본 내 backtest다. 거래비용을 넣지 않은 것이 많다. 각 항목에 적었다.

근거 등급은 1차 보고서와 같다. A: 여러 시장·연구에서 표본 밖 반복. B: 탄탄한 단일 연구나 한 시장 표본 밖. C: 엇갈리거나 비용 무시. D: 반박됨. "미확인"은 이번에 근거를 확인하지 못한 것이다.

## 1. 분류표: 단기 전략 계열과 1차 포함 여부

### Takeaway
카탈로그 여섯 곳을 1차와 대조하면, 1차는 미시구조·장중 시간대·고전 스윙 계열은 거의 다 다뤘다. 빠진 것은 크게 네 묶음이다. **캘린더·발표일 효과**(월말, 요일, 휴장 전, FOMC, 거시지표 발표일, 옵션 만기 주), **이벤트 수요**(실적 발표 premium과 반전, 배당락), **흐름 기반**(일별 공매도, 레버리지 ETF 리밸런싱 수요의 직접 측정), **봉 모양 되돌림**(IBS, pivot point)이다. 옵션·변동성 거래(VIX basis, dispersion)도 빠졌지만 krx-quant에는 맞지 않는다.

### Cited Findings
표의 "1차" 열: 예 = 1차가 다룸, 부분 = 일부만 다룸, 아니오 = 다루지 않음, 범위 밖 = 단기 신호가 아님.

| # | 계열 | 한 줄 규칙 | 카탈로그 항목 | 핵심 논문 | 근거 | 1차 |
|---|---|---|---|---|---|---|
| 1 | Market making, latency arbitrage | 양쪽 호가로 스프레드 수취, μs 속도로 가격 차이 포착 | [151 §3.19](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3247865), [Wiki HFT](https://en.wikipedia.org/wiki/High-frequency_trading) | [Aquilina, Budish, O'Neill 2022](https://www.nber.org/papers/w29011) | 개인 불가 | 예 |
| 2 | Ticker-tape, filter·tick trading | 호가·체결 흐름에서 대량 주문의 시작을 읽고 따라감 | [Wiki HFT "Ticker tape trading"](https://en.wikipedia.org/wiki/High-frequency_trading) | [Cont, Kukanov, Stoikov 2014](https://arxiv.org/abs/1011.6402) | 설명 A, 예측 C | 부분 (OFI는 다룸, iceberg·대량 주문 탐지는 안 다룸) |
| 3 | Rebate trading | 거래소 rebate를 수익원으로 삼는 지정가 매매 | [Wiki Day trading](https://en.wikipedia.org/wiki/Day_trading) | 확인 안 함 | 미확인 | 아니오 |
| 4 | Intraday ETF–ETF arbitrage | 같은 지수 ETF 둘의 호가가 엇갈리면 싼 쪽 매수·비싼 쪽 매도 | [151 §6.4](https://arxiv.org/abs/1912.04492) | 책은 문헌 목록만 | 미확인 | 아니오 |
| 5 | Index arbitrage, cash-and-carry | 선물 basis가 이론가를 벗어나면 현물·선물 반대 매매 | [151 §6.2](https://arxiv.org/abs/1912.04492), [Wiki HFT "Index arbitrage"](https://en.wikipedia.org/wiki/High-frequency_trading) | 1차의 KOSPI200 lead-lag 문헌 | — | 부분 (lead-lag만) |
| 6 | Execution algos (VWAP, TWAP, IS, POV) | 큰 주문을 쪼개 벤치마크 대비 비용 최소화 | [Wiki Algorithmic trading](https://en.wikipedia.org/wiki/Algorithmic_trading), [VWAP](https://en.wikipedia.org/wiki/Volume-weighted_average_price), [Implementation shortfall](https://en.wikipedia.org/wiki/Implementation_shortfall) | — | alpha 아님 | 부분 (VWAP 되돌림만) |
| 7 | News·event arbitrage | 기계가 읽은 뉴스로 즉시 매매 | [Wiki HFT "News-based trading"](https://en.wikipedia.org/wiki/High-frequency_trading) | [Lopez-Lira & Tang](https://arxiv.org/abs/2304.07619) | C | 예 |
| 8 | Intraday momentum, periodicity | 첫 30분 → 마지막 30분, 같은 시간대 지속 | (무료 Quantpedia 목록에 없음) | [Gao et al. 2018](https://papers.ssrn.com/abstract=2440866) | 미국 A, 한국 C | 예 |
| 9 | Overnight anomaly | 종가 매수 → 다음 시가 매도. 또는 갭 반대 | [Quantpedia #0661](https://quantpedia.com/strategies/market-sentiment-and-an-overnight-anomaly) | [Cliff, Cooper, Gulen 2008](https://doi.org/10.2139/ssrn.1004081), [Hendershott, Livdan, Rösch](https://doi.org/10.2139/ssrn.3117663) | 미국 B (비용 제외), 한국 B | 부분 |
| 10 | Opening gap (buy-on-gap) | 전일 저가 아래로 갭 하락한 종목 매수, 종가 청산 | [Chan 2013](https://doi.org/10.1002/9781118676998) (장 위치 미확인) | [비동료심사 재현](https://financialnoob.substack.com/p/buy-on-gap-strategy-and-its-performance) | C (감쇠) | 부분 |
| 11 | Leveraged ETF rebalancing flow | 장중 큰 상승(하락) 뒤 마감 전 같은 방향, 종가 청산 | [151 §4.5](https://arxiv.org/abs/1912.04492), [Chan Ch.7](https://doi.org/10.1002/9781118676998.ch7) | Cheng & Madhavan 2009 (원문 미확인), [QuantRocket 재현](https://quantrocket.com/blog/leveraged-etf-intraday-momentum) | C | 부분 (1차는 원인·비용으로만) |
| 12 | IBS (internal bar strength) | 종가가 일중 범위 하단(IBS < 0.2)이면 다음날 매수, 상단(> 0.8)이면 매도 | [151 §4.4](https://arxiv.org/abs/1912.04492) | [Pandey & Joshi 2023 (preprint)](https://arxiv.org/abs/2306.12434) | C | 아니오 |
| 13 | Pivot point support/resistance | 전일 고·저·종가로 C, R, S를 구해 P > C면 매수, R에서 청산 | [151 §3.14](https://arxiv.org/abs/1912.04492), [Wiki TA "Pivot point"](https://en.wikipedia.org/wiki/Technical_analysis) | 학술 근거 확인 안 함 | 미확인 | 아니오 |
| 14 | MA, channel, breakout, Elder 3-MA | 이동평균 교차, 채널 돌파 | [151 §3.11–3.15](https://arxiv.org/abs/1912.04492), [Wiki Swing trading](https://en.wikipedia.org/wiki/Swing_trading) | [Bajgrowicz & Scaillet 2012](https://ideas.repec.org/a/eee/jfinec/v106y2012i3p473-491.html) | D | 예 |
| 15 | **Turn-of-the-month (TOM)** | 월말 전날 종가 매수 → 새달 3번째 거래일 종가 매도 | [Quantpedia #0041](https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes), [Wiki Market anomaly](https://en.wikipedia.org/wiki/Market_anomaly) | [Lakonishok & Smidt 1988](https://doi.org/10.1093/rfs/1.4.403), [McConnell & Xu](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=917884), 한국 [Lee & Kim 2022](https://ideas.repec.org/a/eee/pacfin/v75y2022ics0927538x22001214.html) | B (비용 미반영) | 아니오 |
| 16 | **Day-of-week, weekend** | 요일별 평균 수익 차이를 이용 | [Wiki Calendar effect](https://en.wikipedia.org/wiki/Calendar_effect) | [Smirlock & Starks 1986](https://doi.org/10.1016/0304-405X%2886%2990011-5) | C/D | 아니오 |
| 17 | **Pre-holiday** (설날·추석 포함) | 휴장 전 거래일만 보유 | [Quantpedia #0083](https://quantpedia.com/strategies/pre-holiday-effect) | [Ariel 1990](https://doi.org/10.1111/j.1540-6261.1990.tb03731.x), [Yuan & Gupta 2014](https://doi.org/10.1016/j.qref.2014.06.001) | C | 아니오 |
| 18 | Payday | 매달 16일만 보유 | [Quantpedia #0419](https://quantpedia.com/strategies/payday-anomaly) | [Ma & Pratt](https://ssrn.com/abstract=3257064) | C | 아니오 |
| 19 | Lunar cycle | 신월 무렵 보유 | [Wiki Calendar effect](https://en.wikipedia.org/wiki/Calendar_effect) | [Yuan, Zheng, Zhu](https://doi.org/10.2139/ssrn.283156) | 미확인 | 아니오 |
| 20 | **Pre-FOMC, FOMC day** | FOMC 전날 종가 매수 → 회의 다음 종가 매도 | [Quantpedia #0075](https://quantpedia.com/strategies/federal-open-market-committee-meeting-effect-in-stocks) | [Lucca & Moench 2015](https://ideas.repec.org/a/bla/jfinan/v70y2015i1p329-371.html), [Kurov et al. 2021](https://doi.org/10.1016/j.frl.2020.101781) | 2015년 전 B, 뒤 D | 아니오 |
| 21 | **Macro announcement days** | 물가·고용·금리 발표일에만 주식 보유 | [151 §19.5](https://arxiv.org/abs/1912.04492) | [Savor & Wilson 2013](https://ideas.repec.org/a/cup/jfinqa/v48y2013i02p343-375_00.html) | B | 아니오 |
| 22 | **Option-expiration week** | 만기 주에만 옵션 활발한 대형주 보유 | [Quantpedia #0102](https://quantpedia.com/strategies/option-expiration-week-effect) | [Stivers & Sun](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1571786) | B (미국 1편) | 부분 (1차는 만기일만) |
| 23 | 장기 계절성 (January, Sell in May, Santa, 대선 주기) | 월·연 단위 | [Wiki Calendar effect](https://en.wikipedia.org/wiki/Calendar_effect) | — | — | 범위 밖 |
| 24 | **Earnings announcement premium** | 발표 예정 종목을 발표 직전·당일 보유 | [Quantpedia #0080](https://quantpedia.com/strategies/earnings-announcement-premium) | [Frazzini & Lamont 2007](https://www.nber.org/papers/w13090), [Barber et al.](https://doi.org/10.2139/ssrn.1872183) | B | 아니오 (1차는 PEAD만) |
| 25 | **Earnings-announcement reversal** | 발표 직전 단기 반전이 커지는 것을 이용 | [Quantpedia #0307](https://quantpedia.com/strategies/reversal-during-earnings-announcements) | [So & Wang 2014](https://doi.org/10.2139/ssrn.2275982) | B | 아니오 |
| 26 | Ex-dividend, dividend capture | 배당락 전후 가격 움직임 이용 | (무료 목록에 없음) | 한국 학술 근거 못 찾음 | 미확인 | 아니오 |
| 27 | Index rebalancing front-running | 지수 리밸런싱 수요를 앞서 매매 | [Wiki Algorithmic trading](https://en.wikipedia.org/wiki/Algorithmic_trading) | [Greenwood & Sammon 2025](https://doi.org/10.1111/jofi.13410) | D | 부분 (MSCI 미조사) |
| 28 | **Short-selling flow** | 일별 공매도가 많은 종목은 이후 약세 | [Quantpedia #0045 (월간 short interest)](https://quantpedia.com/strategies/short-interest-effect-long-short-version) | [Diether, Lee, Werner 2009](https://ideas.repec.org/a/oup/rfinst/v22y2009i2p575-607.html) | B (미국 2005) | 아니오 |
| 29 | Economic links (customer–supplier) | 거래처 주가 움직임 → 공급사의 이후 수익 | — | [Cohen & Frazzini](https://doi.org/10.2139/ssrn.2758776) | B | 부분 (1차 lead-lag는 미국 반도체·선물) |
| 30 | M&A, merger arbitrage | 발표 뒤 인수가와의 스프레드 | [151 §3.16](https://arxiv.org/abs/1912.04492) | — | — | 범위 밖 |
| 31 | Short-term reversal (residual, intraday 분해) | 지난 주 패자 매수·승자 매도 | [Quantpedia #0013](https://quantpedia.com/strategies/short-term-reversal-in-stocks), [151 §10.3](https://arxiv.org/abs/1912.04492) | [Blitz et al. 2024](https://doi.org/10.3905/jpm.2024.1.588), [Miwa 2019](https://doi.org/10.1142/s2010139219500022) | 조건부 B | 예 (Miwa 분해는 새것) |
| 32 | Pairs, stat-arb, OU, Kalman, copula | 스프레드 이탈의 역방향 | [151 §3.8–3.10, 3.18](https://arxiv.org/abs/1912.04492), [ArbitrageLab](https://github.com/hudson-and-thames/arbitragelab) | [Krauss 2017 (WP)](https://www.econstor.eu/bitstream/10419/116783/1/833997289.pdf) | C | 예 |
| 33 | Contrarian + volume·OI filter | 주간 반전에 거래량·미결제약정 변화 필터 | [151 §10.3.1](https://arxiv.org/abs/1912.04492) | 확인 안 함 | 미확인 | 아니오 |
| 34 | Momentum, TSMOM, 52-week high, residual momentum | 과거 승자 지속 | [151 §3.1, 3.7, 10.4](https://arxiv.org/abs/1912.04492), [Quantpedia #0088](https://quantpedia.com/strategies/52-weeks-high-effect-in-stocks) | [Moskowitz, Ooi, Pedersen 2012](https://doi.org/10.1016/j.jfineco.2011.11.003) | 단일 종목 C | 예 |
| 35 | MAX, lottery | 지난달 최대 일수익이 큰 종목 회피 | [Wiki Market anomaly](https://en.wikipedia.org/wiki/Market_anomaly) | [Bali, Cakici, Whitelaw](https://doi.org/10.3386/w14804) | 횡단·월간 | 아니오 |
| 36 | Alpha combos, KNN, ML | 약한 alpha 다수 결합 | [151 §3.17, 3.20](https://arxiv.org/abs/1912.04492) | [Kakushadze 2016](https://arxiv.org/abs/1601.00991) | B | 예 |
| 37 | VRP, volatility targeting | IV > RV면 변동성 매도. 변동성에 반비례해 노출 | [151 §6.5, 7.4](https://arxiv.org/abs/1912.04492), [Quantpedia #0020](https://quantpedia.com/strategies/volatility-risk-premium-effect) | [Moreira & Muir 2017](https://doi.org/10.1111/jofi.12513) | B | 예 |
| 38 | VIX futures basis | basis로 VIX 선물 가격의 이후 변화를 예측 | [151 §7.2](https://arxiv.org/abs/1912.04492), [Quantpedia #0198](https://quantpedia.com/strategies/exploiting-term-structure-of-vix-futures) | [Simon & Campasano](https://doi.org/10.2139/ssrn.2094510) | B (미국) | 아니오 |
| 39 | Dispersion trading | 지수 옵션 매도·구성 종목 옵션 매수 | [151 §6.3](https://arxiv.org/abs/1912.04492), [Quantpedia #0237](https://quantpedia.com/strategies/dispersion-trading) | 확인 안 함 | C | 아니오 |
| 40 | Delta-neutral, gamma scalping | 옵션 보유 + 기초자산 delta 헤지 | [Wiki Algorithmic trading](https://en.wikipedia.org/wiki/Algorithmic_trading), [151 §7.4.1](https://arxiv.org/abs/1912.04492) | 확인 안 함 | 미확인 | 부분 (dealer gamma는 원인으로만) |
| 41 | 옵션 구조 (straddle, condor 등) | payoff 구조 | [151 Ch.2](https://arxiv.org/abs/1912.04492) | — | 신호 아님 | 범위 밖 |

1차에 없던 계열을 묶으면 이렇다.
- 캘린더·발표일: #15–#22.
- 이벤트 수요: #24–#26.
- 흐름 측정: #11(직접 측정), #28.
- 봉 모양 되돌림: #12, #13.
- 파생·인프라: #3, #4, #33, #38–#40.

### Inferences
- 1차는 "어떤 신호가 맞히나"를 중심으로 조사했다. 카탈로그는 "언제 보유하나"(캘린더) 축이 크다. 이 축이 통째로 비었다.
- 새 캘린더 계열은 거의 다 **상승 쪽 drift**다. up/flat/down 적중률에서는 하루 단위 기저율을 조금 옮길 뿐이다. 4분 방향에는 거의 영향이 없다(8절 계산).

### Gaps
- Alpha Architect, Robot Wealth, QuantStart, Quantocracy는 예산 때문에 체계적으로 훑지 못했다. 이들이 다른 계열을 더 담고 있는지는 확인하지 못했다.
- "Advances in Active Portfolio Management"(Grinold & Kahn)의 내용은 확인하지 못했다.

## 2. Kakushadze & Serur (2018) "151 Trading Strategies"의 단기 전략

### Takeaway
이 책은 20장, 자산군별로 전략 규칙과 수식, 참고문헌을 모은 목록이다. 읽은 절(3.9, 3.14, 3.15, 4.4, 4.5, 6.4, 7.2, 10.3, 19.5)에는 성과 수치가 없었다. 규칙과 문헌 목록뿐이다. 옵션 장을 빼면 주식·ETF·지수·변동성·선물·매크로 장에서 일중~수주 전략은 약 30개다. 그중 1차에 없던 것은 pivot point, IBS, LETF decay 매도, ETF 간 일중 arbitrage, dispersion, VIX basis, 거래량·미결제 필터 반전, 경제지표 발표일 매매다.

### Cited Findings
- 서지: Kakushadze & Serur, *151 Trading Strategies*, Palgrave Macmillan 2018 — [DOI](https://doi.org/10.1007/978-3-030-02792-6), [SSRN 3247865](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3247865). 스페인어판 전문 — [arXiv 1912.04492](https://arxiv.org/abs/1912.04492).
- 장 구성: 2 Options, 3 Stocks, 4 ETFs, 5 Fixed Income, 6 Indexes, 7 Volatility, 8 FX, 9 Commodities, 10 Futures, 11 Structured Assets, 12 Convertibles, 13 Tax Arbitrage, 14 Miscellaneous, 15 Distressed, 16 Real Estate, 17 Cash, 18 Cryptocurrencies, 19 Global Macro, 20 Infrastructure — [arXiv 1912.04492 목차](https://arxiv.org/abs/1912.04492).
- 단기(일중~수주) 전략 목록과 규칙 — 모두 [arXiv 1912.04492](https://arxiv.org/abs/1912.04492) 본문·목차:
  - **주식(3장)**: 3.8 Pairs trading. 3.9 Mean-reversion – single cluster, 3.9.1 multiple clusters(업종 평균 대비 수익을 역방향으로 매매). 3.10 weighted regression. 3.11–3.13 MA 1·2·3개. 3.14 Support and resistance. 3.15 Channel. 3.17 KNN single-stock. 3.18 Statistical arbitrage – optimization. 3.19 Market-making. 3.20 Alpha combos.
  - **3.14 Pivot point**: 전일 고가 H, 저가 L, 종가 C로 중심 C = (H + L + C)/3, R = 2C − L, S = 2C − H. 현재가 P > C면 매수하고 P ≥ R에서 청산한다. P < C면 매도하고 P ≤ S에서 청산한다.
  - **ETF(4장)**: 4.4 Mean-reversion. 전일 IBS = (C − L)/(H − L)로 ETF를 줄 세워 상위 decile 매도, 하위 decile 매수(dollar-neutral). 4.5 Leveraged ETFs. LETF는 매일 리밸런싱하므로 오른 날 사고 내린 날 판다. 장기적으로 음의 drift가 생긴다. 같은 지수·같은 배율의 LETF와 inverse LETF를 함께 공매도해 이 drift를 거둔다. 한쪽이 크게 오르면 단기 손실 위험이 크다.
  - **지수(6장)**: 6.2 Cash-and-carry arbitrage. 6.3 Dispersion trading(6.3.1 부분 포트폴리오). 6.4 Intraday arbitrage between index ETFs. 같은 지수 ETF 둘의 bid와 ask가 문턱 이상 엇갈리면 싼 쪽을 사고 비싼 쪽을 판다(예: SPY 대 IVV). 6.5 Index volatility targeting.
  - **변동성(7장)**: 7.2 VIX futures basis trading. 본문은 basis가 VIX 자체의 이후 변화는 예측하지 못하지만 VIX 선물 가격의 이후 변화는 크게 예측한다고 요약한다(되돌림 전략). 7.3 Volatility carry with two ETNs. 7.3.1 Hedging short VXX with VIX futures. 7.4 VRP, 7.4.1 VRP with gamma hedging. 7.5 Volatility skew – long risk reversal. 7.6 Variance swaps.
  - **선물(10장)**: 10.2 Calendar spreads. 10.3 Contrarian trading. 지난 주 수익률에서 시장 평균을 뺀 값의 반대로 비중을 정하고 매주 리밸런싱한다. 10.3.1은 지난 주와 그 전 주의 거래량·미결제약정 변화를 필터로 더한다. 10.4 Trend following.
  - **글로벌 매크로(19장)**: 19.5 Trading with economic announcements. FOMC 같은 주요 발표일에만 주식(ETF·선물)을 보유하고 다른 날은 무위험자산에 둔다.
  - **옵션(2장)**: 2.2 Covered call부터 2.57 Bullish long seagull spread까지 payoff 구조다. 신호 규칙이 아니다.
- 단기가 아닌 것: 3.1–3.7(가격·이익 momentum, value, low-vol, implied volatility, multifactor, residual momentum), 4.1–4.3과 4.6(섹터 rotation, R-squared, 다자산 추세), 3.16(M&A) — [arXiv 1912.04492 목차](https://arxiv.org/abs/1912.04492).

### Inferences
- 이 책은 근거의 강도를 매기지 않는다. 각 규칙은 따로 검증해야 한다. "151개"라는 숫자는 근거의 양이 아니다.
- 세 종목 엔진에 바로 옮길 수 있는 것은 시계열 형태의 규칙이다. pivot point, IBS(종목별 시계열로 바꾸면), 경제지표 발표일, LETF 리밸런싱 흐름이다. 횡단 decile 규칙(4.4, 3.9)은 세 종목에서는 순위가 너무 거칠다.

### Gaps
- 영어판 원문 PDF는 받지 못했다. 스페인어판과 절 번호가 같다고 가정했다.
- 3.14 수식의 "/3"은 PDF 텍스트 추출에서 빠졌다. 표준 pivot 정의로 보충했다.

## 3. Quantpedia의 단기·계절성 전략

### Takeaway
무료로 볼 수 있는 Quantpedia 목록은 82개다. 그중 일·주 단위 주식 전략은 TOM, FOMC, 휴장 전, 옵션 만기 주, payday, overnight + sentiment, 단기 반전, 실적 발표 premium·반전, pairs다. 상세 페이지의 성과는 원 논문 표본 내 수치다. 거래비용은 대개 보고되지 않았다. 보유일이 적은 전략은 쉬는 날에 현금 수익 4%를 가정해 연 수익을 부풀린다.

### Cited Findings
- 무료 목록(screener) 82개. "seasonality"와 "intraday" 키워드를 줘도 같은 82개가 나왔다. 키워드 필터가 아니라 무료 목록으로 보인다. 일·주 단위 주식 전략은 #0012 Pairs Trading with Stocks, #0013 Short Term Reversal, #0041 TOM, #0055 Pairs Trading with Country ETFs, #0071 Short Term Reversal with Futures, #0075 FOMC, #0080 Earnings Announcement Premium, #0083 Pre-Holiday, #0102 Option-Expiration Week, #0144 Trend-following in Stocks, #0198 VIX term structure, #0238 Reversal in PEAD, #0307 Reversal During Earnings-Announcements, #0419 Payday, #0661 Market Sentiment and an Overnight Anomaly, #0713 Synthetic Lending Rates. #0753은 Bitcoin overnight이다 — [Quantpedia screener](https://quantpedia.com/screener?FilterKeywords=seasonality&screener_tab=screener-classic).
- **TOM (#0041)**: 월말 전 거래일에 SPY 매수, 새달 3번째 거래일 종가 매도. 1926–2005, 지표 연 7.2%, 변동성 6.9%, Sharpe 1.04. 비용은 보고하지 않았다. 원 논문은 시장 초과수익 전부가 4일 TOM 구간에 났다고 썼다. 1987–2005는 발견 이후 표본으로 쓰였다. 발견은 Lakonishok & Smidt(DJIA 1897–1986) — [Quantpedia](https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes), [McConnell & Xu SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=917884), [Lakonishok & Smidt 1988](https://doi.org/10.1093/rfs/1.4.403).
- **FOMC (#0075)**: 회의 전날 종가 매수, 회의 다음 종가 매도. 연 약 8일만 보유. 1980–2000 지표 연 6.19%는 쉬는 날 현금 4%를 가정한 값이다. MDD −8.74%, 비용 미보고. 1960–1979에는 효과가 없었다 — [Quantpedia](https://quantpedia.com/strategies/federal-open-market-committee-meeting-effect-in-stocks).
- **Pre-Holiday (#0083)**: 미국 공휴일 약 10개의 전 거래일만 보유. 1896–2002 DJIA 평균 0.239%/일에서 만든 지표 연 6.39%(현금 4% 포함). Quantpedia 스스로 DJIA·대형주에서는 1980년대 말~1990년대 초 이후 대체로 사라졌고 소형주에서만 견고하다고 적었다 — [Quantpedia](https://quantpedia.com/strategies/pre-holiday-effect).
- **Option-Expiration Week (#0102)**: S&P 100 종목을 만기 주에만 보유. 1988–2010, 만기 주 12번에서 6.3% + 현금 4% 가정 = 지표 연 9.3%, Sharpe 0.61. 1996–2008의 두 부분기간 스프레드는 67.9bp와 70.7bp였다. 비용 미보고 — [Quantpedia](https://quantpedia.com/strategies/option-expiration-week-effect), [Stivers & Sun SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1571786).
- **Payday (#0419)**: 매달 16일 S&P 500 보유. 1980–2010, 지표 연 2.57%, t = 3.07, Sharpe 0.6. 비용 미보고 — [Quantpedia](https://quantpedia.com/strategies/payday-anomaly).
- **Market Sentiment and an Overnight Anomaly (#0661)**: SPY가 20일 MA 위, VIX가 20일 MA 아래, Brain sentiment가 20일 MA 위일 때만 SPY overnight 보유. 2018–2021, 연 15.58%, Sharpe 2.12. **비용은 계산에서 뺐고**, 저자도 비용이 클 것이라 썼다 — [Quantpedia](https://quantpedia.com/strategies/market-sentiment-and-an-overnight-anomaly), [Vojtko & Hanicová SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3829582).
- **Short Term Reversal (#0013)**: 미국 대형 100종목, 주간 패자 10 매수·승자 10 매도. 1990–2009, Nomura 비용 추정을 넣은 순수익 주 29.0bp(연 약 16.25%). 같은 비용으로 500종목은 10.6bp, 1,500종목은 −31.4bp였다. 대형주에서만 살아남았다. 비용을 넣은 드문 페이지다 — [Quantpedia](https://quantpedia.com/strategies/short-term-reversal-in-stocks), [Groot, Huij, Zhou SSRN](http://papers.ssrn.com/sol3/papers.cfm?abstract_id=1605049).
- 같은 페이지는 Miwa의 결과를 인용한다. 단기 반전은 overnight 수익이 아니라 **장중 수익**에서 나온다. 장중 수익은 다음 주에 되돌고 overnight 수익은 되돌지 않는다. 비유동 종목과 변동성 높은 시기에 강하다 — [Miwa 2019](https://doi.org/10.1142/s2010139219500022).
- 실적 발표 기간에는 단기 반전이 평소의 **6배**다. 발표 전 재고 위험 때문에 시장조성자가 더 큰 보상을 요구한다는 해석이다 — [So & Wang (SSRN 초록)](https://doi.org/10.2139/ssrn.2275982).
- Earnings announcement premium: 예정된 실적 발표일 무렵 주가가 오른다. 과거 발표 기간 거래량이 컸던 종목에서 크다. 저자는 주의를 끈 소액투자자 매수로 해석했다 — [Frazzini & Lamont, NBER w13090](https://www.nber.org/papers/w13090). Quantpedia #0080 지표는 연 18.36%, 변동성 16.12%(월간 리밸런싱)다 — [Quantpedia screener](https://quantpedia.com/screener?FilterKeywords=seasonality&screener_tab=screener-classic).
- 2026년 Quantpedia 글: 아침 거래 활동 지표가 며칠 연속 약해진 뒤 SPY 반전(2026-09-24), 1분 SPY 추세추종(2026-09-22), 섹터 ETF의 월중 momentum cycle(1998-12~2026-06, long-short 연 5.99%, Sharpe 0.55, 비용 언급 없음) — [Quantpedia seasonality 태그](https://quantpedia.com/tag/seasonality/). "Lunch Effect in the U.S. Stock Market Indices"는 제목만 확인했다 — [Quantpedia](https://quantpedia.com/lunch-effect-in-the-u-s-stock-market-indices/).

### Inferences
- Quantpedia의 "indicative performance"는 원 논문 표에서 다시 계산한 표본 내 수치다. 실거래 기록이 아니다.
- 보유일이 1년에 8~12일뿐인 전략(FOMC, 휴장 전, 만기 주)은 연 수익의 상당 부분이 가정한 현금 4%다. 효과 자체는 연 2~6% 정도다(위 수치에서 4%를 빼면).
- 거래비용을 넣은 단기 반전 결과는 universe가 커질수록 나빠진다. 세 대형주만 보는 krx-quant에는 오히려 유리한 쪽이다.

### Gaps
- 유료 페이지(overnight anomaly 본 페이지, lunar cycle, leveraged ETF 관련)는 열리지 않았다. 원 논문과 성과를 확인하지 못했다.
- Quantpedia 전체 목록에 단기 전략이 몇 개인지는 알 수 없다(무료 82개만 봤다).

## 4. Wikipedia(영어·한국어)가 나열한 전략 유형

### Takeaway
영어 Wikipedia가 나열한 전략 유형 가운데 1차에 없던 것은 index fund 리밸런싱 선행, delta-neutral, rebate trading, ticker-tape·filter trading(대량 주문 탐지), iceberg sniffing 같은 실행 쪽 기법, 그리고 캘린더 효과(weekend, midweek, TOM)다. 한국어 문서는 대부분 짧은 토막글이라 새 유형이 없다.

### Cited Findings
- Algorithmic trading의 Strategies 절: Trading ahead of index fund rebalancing, Pairs trading, Delta-neutral strategies, Arbitrage, Mean reversion, Scalping, Transaction cost reduction(VWAP, TWAP, Implementation shortfall, POV, Display size, Liquidity seeker, Stealth, 상대편 iceberg 주문을 찾는 "sniffing" 알고리즘), dark pool 전용 전략, Market timing. HFT 하위로 Market making, Statistical arbitrage, Event arbitrage, Spoofing, Quote stuffing — [Wikipedia](https://en.wikipedia.org/wiki/Algorithmic_trading).
- 같은 문서에는 "non-ergodicity"와 "Binomial Evolution Function" 절이 있다. `{{fact|date=May 2026}}` 출처 요구 표시가 붙어 있다. 근거로 쓰지 않는다 — [Wikipedia](https://en.wikipedia.org/wiki/Algorithmic_trading).
- High-frequency trading의 Strategies 절: Market making, Quote stuffing, Ticker tape trading(filter trading, 대량 주문 시작을 알아채는 tick trading), Statistical arbitrage, Index arbitrage, News-based trading, Low-latency strategies, Order properties strategies — [Wikipedia](https://en.wikipedia.org/wiki/High-frequency_trading).
- Day trading의 Techniques 절: Swing trading, Trend following, Contrarian investing, Range trading, Scalping, Rebate trading, Trading the news, Price action trading(빈 절), Market-neutral trading, Algorithmic trading — [Wikipedia](https://en.wikipedia.org/wiki/Day_trading).
- Swing trading: 규칙 기반 접근의 예로 Elder의 이동평균 3개 전략을 든다 — [Wikipedia](https://en.wikipedia.org/wiki/Swing_trading).
- Calendar effect 예시: Sell in May, January effect, January barometer, Mark Twain effect, Congressional effect, Santa Claus rally, Super Bowl indicator, 대선 주기, Weekend effect(미국 주말 수익 장기 평균이 음), Midweek effect(월요일 종가→수요일 종가) — [Wikipedia](https://en.wikipedia.org/wiki/Calendar_effect).
- Market anomaly의 학술 anomaly 표는 154행이다. 151행이 횡단(cross-sectional)이고 시계열은 3개(Weekend Effect – Smirlock & Starks 1986, January effect – Keim 1985, Turn of the Month – Agrawal & Tandon 1994)다. 단기 쪽 횡단 항목은 Short term reversal(Jegadeesh 1989), Maximum return over month(Bali, Cakici, Whitelaw), 52 week high, Momentum and Volume, Earnings announcement return, Short Interest, Option Volume, Return Seasonality(Heston & Sadka 2008)다 — [Wikipedia](https://en.wikipedia.org/wiki/Market_anomaly).
- Technical analysis의 overlay 목록: Bollinger bands, Channel, Ichimoku kinko hyo, Moving average, Parabolic SAR, Pivot point, Resistance, Support, Trend line — [Wikipedia](https://en.wikipedia.org/wiki/Technical_analysis).
- Statistical arbitrage는 Niederhoffer & Osborne(1966) "Market Making and Reversal on the Stock Exchange"를 시초로 든다 — [Wikipedia](https://en.wikipedia.org/wiki/Statistical_arbitrage). Pairs trade는 distance, cointegration, copula와 ARMA 스프레드 예측을 든다 — [Wikipedia](https://en.wikipedia.org/wiki/Pairs_trade).
- [Mean reversion (finance)](https://en.wikipedia.org/wiki/Mean_reversion_%28finance%29)와 [Momentum (finance)](https://en.wikipedia.org/wiki/Momentum_%28finance%29)는 짧은 문서다(원문 각 4.7KB, 3.5KB). "Order flow" 제목의 영어 문서는 받지 못했다(없거나 다른 제목).
- 한국어: [알고리즘 트레이딩](https://ko.wikipedia.org/wiki/알고리즘_트레이딩)은 추세추종, 역추세, 통계적 차익거래, 페어트레이딩만 다룬다. [초단타 매매](https://ko.wikipedia.org/wiki/초단타_매매)는 영어 HFT 문서와 같은 전략 절을 가진 번역이다. [스캘핑](https://ko.wikipedia.org/wiki/스캘핑)은 매매마다 세금이 붙는 국내 주식에서는 성공 확률이 낮다고 쓰지만 출처 요구 표시가 붙어 있다. [데이 트레이딩](https://ko.wikipedia.org/wiki/데이_트레이딩), [스윙 트레이딩](https://ko.wikipedia.org/wiki/스윙_트레이딩), [캘린더 효과](https://ko.wikipedia.org/wiki/캘린더_효과), [시장 이상현상](https://ko.wikipedia.org/wiki/시장_이상현상)은 토막글이다. 통계적 차익거래, 페어 트레이딩, 평균 회귀, 시장 미시구조, VWAP 한국어 문서는 받지 못했다.

### Inferences
- Wikipedia는 근거 등급을 주지 않는다. 유형 목록으로만 쓸 수 있다.
- 학술 anomaly 표가 거의 다 횡단이라는 점은 중요하다. 세 종목에서 쓰려면 시계열 형태로 바꿔야 한다. 바꿀 수 있는 것은 short interest(일별 공매도 흐름), earnings announcement return(이벤트 플래그), MAX(종목 자기 극단 수익 뒤 약세) 정도다.

### Gaps
- Market microstructure, Order flow 관련 영어 문서에서 새 전략 유형은 찾지 못했다.

## 5. 다른 카탈로그: Chan의 책, Hudson & Thames ArbitrageLab

### Takeaway
Chan(2013)의 장 구성은 Crossref로 확인했다. 장중 momentum 장의 LETF 전략은 표본 밖 재현에서 감쇠했다. ArbitrageLab은 pairs·stat-arb 방법을 Krauss 분류 전체로 구현한 오픈소스다. 계열로는 1차의 pairs·stat-arb와 같다. 방법 단위로는 OU 최적 문턱, regime-switching arbitrage rule, sparse mean-reverting portfolio, vine copula가 새것이다.

### Cited Findings
- Chan, *Algorithmic Trading: Winning Strategies and Their Rationale* (Wiley 2013) 장 제목: 1 Backtesting and Automated Execution, 2 The Basics of Mean Reversion, 3 Implementing Mean Reversion Strategies, 4 Mean Reversion of Stocks and ETFs, 5 Mean Reversion of Currencies and Futures, 6 Interday Momentum Strategies, 7 Intraday Momentum Strategies, 8 Risk Management — [Crossref DOI](https://doi.org/10.1002/9781118676998), [4장](https://doi.org/10.1002/9781118676998.ch4), [7장](https://doi.org/10.1002/9781118676998.ch7).
- Buy-on-gap: 전일 저가보다 (90일 수익률 표준편차) 1배 이상 낮게 시작하고, 시가가 20일 MA 위인 종목 중 가장 많이 빠진 최대 10개를 사서 종가에 판다. 재현 블로그는 성과가 시간이 지나며 나빠졌다고 썼다 — [financialnoob (비동료심사)](https://financialnoob.substack.com/p/buy-on-gap-strategy-and-its-performance). 책의 어느 장인지는 확인하지 못했다.
- LETF 장중 momentum(Chan 7장): 14:00에 전일 종가 대비 움직임이 문턱을 넘으면 같은 방향으로 들어가 종가에 청산한다. DRN은 Chan의 표본(2011–2012)에서는 좋았지만 2013년 이후 비용 뒤 손실이었다. LETF 14개에 ±6% 문턱이면 2008–2016 비용 뒤 CAGR 약 31%, Sharpe 1.95였지만 2017년부터 평탄해졌다. 비용 가정은 명시하지 않았다. ±6%는 2·4·6·8%를 훑어 고른 값이다 — [QuantRocket](https://quantrocket.com/blog/leveraged-etf-intraday-momentum).
- LETF 리밸런싱 규모는 배율 L, 지수 수익 r, 순자산 NAV일 때 NAV × (L² − L) × r다. 2배는 2·NAV·r, −2배는 6·NAV·r이다. 둘 다 오른 날 사고 내린 날 판다 — [Tsalikis 2018 (검색 요약 기준, PDF 403)](https://businessperspectives.org/images/pdf/applications/publishing/templates/article/assets/10375/imfi_2018_02_Tsalikis.pdf). 산식은 배율 정의에서 바로 나온다.
- 반론 쪽 논문은 제목만 확인했다: "Are Concerns About Leveraged ETFs Overblown?" — [Ivanov & Lenkey (SSRN)](https://doi.org/10.2139/ssrn.2544657). 장중 변동성과의 관계: [Shum et al. (SSRN)](https://doi.org/10.2139/ssrn.2161057).
- ArbitrageLab은 Krauss의 pairs trading 분류 전체를 다룬다고 밝힌 오픈소스 Python 라이브러리다 — [GitHub README](https://github.com/hudson-and-thames/arbitragelab), [Krauss 분류](https://www.econstor.eu/bitstream/10419/116783/1/833997289.pdf). 모듈 구성 — [GitHub](https://github.com/hudson-and-thames/arbitragelab):
  - codependence: correlation, GNPR distance, information, optimal transport.
  - cointegration_approach: Engle-Granger, Johansen, minimum profit, multi-cointegration, sparse mean-reverting portfolio.
  - copula_approach: Archimedean, elliptical, mixed copula, vine copula partner selection·strategy.
  - distance_approach: basic distance, Pearson distance.
  - hedge_ratios: ADF-optimal, Box-Tiao, half-life, Johansen, linear.
  - ml_approach: OPTICS·DBSCAN 군집, regressor committee, TAR, neural networks.
  - optimal_mean_reversion: OU, XOU, CIR, heat potentials.
  - other_approaches: Kalman filter, PCA.
  - stochastic_control_approach: Jurek, Mudchanatongsuk, optimal convergence.
  - time_series_approach: ARIMA, H-strategy, OU optimal threshold(Bertram, Zeng), quantile time series, regime-switching arbitrage rule.
  - 문서 사이트(readthedocs)는 이번 조회에서 404였다.

### Inferences
- 세 종목에서 pairs는 삼성전자–SK하이닉스 한 쌍(과 삼성전기)뿐이다. ArbitrageLab의 방법은 이 한 스프레드에 대한 진입 문턱(OU 최적 문턱)과 국면 규칙(regime-switching) 설계에 쓸 수 있다. 다만 1차 결론(pairs는 비용 뒤 C)은 그대로다.
- ±6% 문턱은 네 값 중 사후에 고른 것이라 Sharpe 1.95에는 선택 편향이 섞여 있다.
- LETF 전략의 표본 밖 감쇠는 "구조적 흐름이라도 알려지면 가격에 미리 들어간다"는 사례다.

### Gaps
- Chan의 다른 책(*Quantitative Trading*, *Machine Trading*)의 전략 목록은 확인하지 못했다.
- Cheng & Madhavan(2009) 원문은 열지 못했다.

## 6. 한국 고유 캘린더·이벤트 효과

### Takeaway
한국에서 근거가 가장 나은 캘린더 효과는 **TOM**이다. 외국인이 월말·월초에 더 사고, 그 종목이 더 오른다(2022). 요일과 휴장 효과는 KOSPI에서 약하거나 없다. 2000–2025 연구는 캘린더 효과가 짧게 나타났다 사라지고 최근 약해진다고 본다. 설날 전 상승은 아시아 7개 시장(한국 포함) 연구가 있지만 KOSPI만 본 연구는 유의하지 않았다. 배당락은 학술 근거를 찾지 못했다. 수능일 개장 시각 이동은 엔진의 시간대 피처에 직접 영향을 준다.

### Cited Findings
- **TOM**: 한국 시장의 TOM 효과는 통계적으로 유의하다. size, 연초, 분기초, 지수 리밸런싱으로 설명되지 않는다. 개인·기관은 TOM에 더 사지 않는다. 개인이 상대적으로 많이 산 종목은 부진하다. 외국인은 TOM에 더 많이 사고, 그 종목이 초과수익을 낸다. 저자 결론은 외국인이 TOM 효과에 일부 기여한다는 것이다 — [Lee & Kim 2022, Pacific-Basin Finance Journal](https://ideas.repec.org/a/eee/pacfin/v75y2022ics0927538x22001214.html). 창 정의, 크기, 표본기간은 초록에 없다.
- 코스닥 1996–2013 일별 자료에서 TOM 효과는 양이고 연·월·사건별로 일관됐다. 코스피보다 컸고 해마다 덜 안정적이었다 — [홍건표·임인섭·오현탁 2015, 한국산학기술학회논문지](https://koreascience.kr/article/JAKO201507964683903.do).
- **요일**: 1985–2004 한국·홍콩·일본에서 요일 효과와 금요일 효과는 있었고 월요일 효과는 뚜렷하지 않았다 — [Emerald 2008 (검색 요약 기준, 본문 미확인)](https://emerald.com/insight/content/doi/10.1016/S0196-3821%2807%2900207-9).
- **2000–2025 종합**: 주·월·휴장 효과는 특정 창에서 뚜렷하지만 변동이 크고 짧았다. 최근 전반적으로 약해졌다. 수치는 초록에 없다 — [Song 2026, ICFIED](https://www.atlantis-press.com/proceedings/icfied-26/126023603).
- **휴장**: KOSPI와 JASDAQ에서 휴장 효과는 유의하지 않았다. KOSDAQ은 문화적 휴일 뒤(post-holiday) 효과만 있었다 — [Segaran & Chia 2021](https://eprints.ums.edu.my/id/eprint/31770).
- **설날(Lunar New Year)**: 1999-09~2012-03 아시아 7개 시장(한국 포함)에서 설 연휴 전 수익이 모두 유의하게 양이었다. 한국만의 계수는 보도자료에 없다 — [Griffith 대학 보도자료](https://news.griffith.edu.au/2014/09/26/moon-shines-new-light-on-asian-markets/), [Yuan & Gupta 2014](https://doi.org/10.1016/j.qref.2014.06.001).
- **휴장 전 효과의 원 결과(미국)**: 1963–1982 시장 수익의 1/3 넘게가 해마다 휴장 전 8거래일에 났다. 휴장 전 평균 수익은 다른 날의 9~14배였다 — [Ariel 1990 (초록)](https://doi.org/10.1111/j.1540-6261.1990.tb03731.x).
- **옵션 만기**: 1차가 만기일 효과를 다뤘다(Park & Lim 2003 등). 만기 "주" 효과의 한국 연구는 찾지 못했다.
- **FOMC와 한국**: 전 세계 주식시장은 FOMC 회의 무렵에 높은 수익을 낸다. 다른 중앙은행에는 그런 결과가 없다. 자국 중앙은행도 마찬가지다. FOMC 발표 뒤 전 세계 불확실성이 줄어든다 — [Brusa, Savor, Wilson 2019 (초록)](https://doi.org/10.1093/rof/rfz015). Lucca & Moench는 다른 주요국 지수에도 비슷한 pre-FOMC 수익이 있었다고 썼다 — [RePEc 초록](https://ideas.repec.org/a/bla/jfinan/v70y2015i1p329-371.html). 그러나 미국 pre-FOMC drift는 2015년 이후 사실상 사라졌다(2019-12까지 연장 표본) — [Kurov, Wolfe, Gilbert 2021](https://doi.org/10.1016/j.frl.2020.101781).
- **배당락**: 2023-12 기사에서 유안타증권은 과거 코스피가 이론 배당락 지수보다 높게 시작하는 경향이 있었다고 분석했다 — [뉴시스 2023-12-26](https://www.newsis.com/view/NISX20231226_0002570439). 2023년 배당기준일 제도 변경으로 연말 배당락 효과가 예년보다 약할 것이라는 전망이 있었다 — [한경 유레카](https://eureka.hankyung.com/insight/detail/3859). "배당락 전 5일 매수, 배당락일 매도" 패턴이 사라질 것이라는 칼럼도 있다 — [인베스팅닷컴 칼럼](https://kr.investing.com/analysis/article-200450375). 모두 언론·칼럼이고 학술 실증은 찾지 못했다.
- **수능일**: 2025-11-13 수능일에 유가증권·코스닥 정규장은 10:00에 열었고(파생 09:45) 마감도 1시간 늦췄다. 넥스트레이드는 프리마켓을 열지 않았다. 그날은 옵션 만기일과 겹쳤다 — [헤럴드경제 2025-11-12](https://www.heraldk.com/article/2025111215214040899), [SBS Biz](https://biz.sbs.co.kr/amp/article/20000199183).
- **데이터 마이닝 경고**: 100년 일별 자료에서 개별 캘린더 규칙의 명목 p값은 매우 유의하다. 그러나 그 규칙이 뽑혀 나온 규칙 전체를 고려하면 캘린더 효과는 유의하지 않다 — [Sullivan, Timmermann, White 2001](https://doi.org/10.1016/S0304-4076%2801%2900077-X), [FMG 토론문 초록 (검색 요약 기준)](https://www.fmg.ac.uk/publications/discussion-papers/dangers-data-driven-inference-case-calender-effects-stock-returns). Hansen & Lunde도 캘린더 효과 유의성 검정을 다룬다 — [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=388601).

### Inferences
- 한국 TOM은 외국인 매수와 이어진다. 엔진은 투자자별 수급 10년을 갖고 있다. 그래서 "TOM × 외국인 순매수"를 직접 시험할 수 있다. 다른 캘린더 효과보다 메커니즘이 분명하다.
- 설날·추석은 1년에 두 번이다. 10년이면 각 10번, 합쳐 20번이다. 적중률 표준오차가 약 11%p라 사실상 검증할 수 없다.
- FOMC 성명은 한국 시각 새벽에 나온다. 그러므로 한국에서 "FOMC 무렵"은 발표 전 세션과 발표 다음 시가 갭으로 나뉜다. Brusa et al.의 결과는 한국은행 금통위 발표일에는 비슷한 premium을 기대하기 어렵다는 쪽이다.

### Gaps
- 설날·추석 효과를 한국 시장만 본 KCI 논문은 찾지 못했다. RISS·DBpia에서 "연휴효과"로 다시 찾아볼 만하다.
- 한국 옵션 만기 주(월물·위클리) 효과, 배당락일 가격 하락률, MSCI 분기 리밸런싱일의 대형주 효과에 대한 연구를 찾지 못했다.
- 새해 첫 거래일 개장 시각 변경은 이번에 확인하지 않았다.

## 7. 새 계열 짧은 노트

### Takeaway
새 계열 가운데 근거가 B 이상인 것은 거시지표 발표일, TOM, 실적 발표 premium·반전, 일별 공매도 흐름, 옵션 만기 주다. 모두 미국 위주이고 대부분 비용을 넣지 않았다. pre-FOMC는 2015년 이후 사라졌다. 휴장 전, 요일, payday, lunar는 C~미확인이다.

### Cited Findings
- **Macro announcement days**: 1958–2009 물가·실업·금리 뉴스가 예정된 날의 평균 초과수익은 11.4bp, 다른 날은 1.1bp였다. 발표일이 연간 누적 주식 위험 premium의 60% 넘게를 차지했다. Sharpe는 약 10배였다 — [Savor & Wilson 2013 (RePEc 초록)](https://ideas.repec.org/a/cup/jfinqa/v48y2013i02p343-375_00.html). 151 §19.5가 이를 매매 규칙으로 정리했다 — [arXiv 1912.04492](https://arxiv.org/abs/1912.04492).
- **Pre-FOMC drift**: 미국 주식은 예정된 FOMC 결정 전에 큰 초과수익을 냈다. 국채와 단기금리 선물에는 없었다. 다른 주요 미국 거시지표에는 발표 전 수익이 없었다 — [Lucca & Moench 2015 (초록)](https://ideas.repec.org/a/bla/jfinan/v70y2015i1p329-371.html). 2015년 이후 사라졌고 불확실성 감소가 원인일 수 있다 — [Kurov, Wolfe, Gilbert 2021](https://doi.org/10.1016/j.frl.2020.101781).
- **TOM**: 3절과 6절. 미국은 1897년부터 반복됐고 한국도 유의하다. 월말 기관 현금 수요(month-end liquidity)로 설명하는 논문도 있다(제목만 확인) — [Etula et al. "Dash for Cash"](https://doi.org/10.2139/ssrn.2528692).
- **Earnings announcement premium과 반전**: 3절. 실적 발표 무렵 주가가 오르고, 발표 기간에는 단기 반전이 6배로 커진다 — [Frazzini & Lamont](https://www.nber.org/papers/w13090), [So & Wang](https://doi.org/10.2139/ssrn.2275982). 1차가 다룬 PEAD(대형주에서 소멸)와는 다른 현상이다. 발표 "전후 며칠"의 수익과 반전이다.
- **Short-selling flow**: 2005년 미국 SEC 자료에서 공매도는 NYSE 거래량의 24%, Nasdaq의 31%였다. 공매도자는 양의 수익 뒤에 더 팔고, 이후의 음의 초과수익을 맞혔다. 일별 공매도 활동에 기반한 전략은 표본기간에 유의한 양의 수익을 냈다. 저자는 단기 과잉반응을 노린 매매로 해석했다 — [Diether, Lee, Werner 2009 (RePEc 초록)](https://ideas.repec.org/a/oup/rfinst/v22y2009i2p575-607.html). 표본은 1년이다.
- **Option-expiration week**: 3절. 미국 S&P 100에서 두 부분기간 모두 양이었다 — [Stivers & Sun](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1571786).
- **IBS**: 종가가 일중 범위 하단(IBS < 0.2)이면 다음날 오르고 상단(> 0.8)이면 내린다는 주장이다. 원 주장(Pagonidis, 2013-05까지 ETF 일봉)은 찾지 못했다. 국가 ETF 10년 재현 preprint가 유용한 지표라고 보고했다. 비용·표본 밖 분석은 찾지 못했다 — [Pandey & Joshi 2023 (arXiv)](https://arxiv.org/abs/2306.12434).
- **Overnight 분해**: 단기 반전은 장중 수익에서 나온다(3절 Miwa). 1차가 다룬 한국 overnight–장중 음의 관계(Ham et al. 2023)와 같은 방향이다.
- **VIX futures basis, dispersion**: 2절. 미국 상장 VIX 선물·ETN과 개별주 옵션 유동성이 필요하다.

### Inferences
- 새 계열 대부분은 "언제 보유하나"를 바꾸는 저빈도 신호다. 1년에 8~24번 나오는 사건이라 1년 분봉으로는 검증할 수 없다. 10년 일봉·수급으로만 볼 수 있다.
- 발표일 효과(Savor & Wilson, FOMC)는 위험 보상 설명이 강하다. 위험 보상이면 방향 적중률보다 평균 수익에 나타난다. 적중률 우선 평가에서는 작게 보일 것이다.

### Gaps
- 거시지표 발표일 premium이 한국 주식(특히 반도체 대형주)에서도 나는지에 대한 연구를 찾지 못했다.
- 한국 일별 공매도 흐름의 수익 예측력 연구를 찾지 못했다. 공매도는 2023-11~2025-03 금지됐으므로 쓸 수 있는 최근 이력도 짧다(1차 노트 08).

## 8. krx-quant 적용 메모

### Takeaway
세 종목과 엔진 데이터에 맞는 새 계열은 여섯 가지다. 대부분 독립 방향 신호가 아니라 **조건 피처와 평가 층화**로 쓰는 것이 맞다. 우선순위는 (1) 세션·캘린더 플래그, (2) 단일종목 LETF 리밸런싱 수요, (3) TOM × 외국인 수급, (4) 실적 발표 창, (5) IBS·종가 위치, (6) 일별 공매도 흐름이다. 맞지 않는 것은 dispersion, VIX basis, ETF 간 arbitrage, rebate, M&A, payday, lunar, 장기 계절성이다.

### Cited Findings
아래 각 항목의 근거는 앞 절의 출처를 따른다.
1. **세션·캘린더 플래그 묶음**. 넣을 것: TOM(월말 전날~새달 3번째 거래일), 휴장 전후(설날·추석·연말), 옵션 만기 주와 만기일, FOMC 다음 한국 세션, 미국 CPI·고용 발표 다음 세션, 금통위 당일, 수능일, MSCI 리밸런싱일. 쓰는 곳: LightGBM 피처, HAR 변동폭 조건, 그리고 평가에서 이 날들을 따로 집계하는 층화. 수능일에는 정규장이 10:00~16:30으로 밀린다. 그래서 "장 시작 후 n분" 피처와 "시계 시각" 피처가 어긋난다. 시간대 계절성(Mandelbrot, periodicity)은 장 시작 기준 시각으로 맞춰야 한다 — [헤럴드경제](https://www.heraldk.com/article/2025111215214040899). 데이터 마이닝 경고 때문에 플래그를 하나씩 더할 때마다 시도 수 N을 세어야 한다 — [Sullivan, Timmermann, White 2001](https://doi.org/10.1016/S0304-4076%2801%2900077-X).
2. **단일종목 LETF 리밸런싱 수요 (005930, 000660)**. 예상 종가 리밸런싱 = Σ AUMᵢ × (Lᵢ² − Lᵢ) × r_t. 2배 상품은 2·AUM·r, −2배 인버스는 6·AUM·r다. 둘 다 오른 날 사고 내린 날 판다 — [산식 출처 (검색 요약 기준)](https://businessperspectives.org/images/pdf/applications/publishing/templates/article/assets/10375/imfi_2018_02_Tsalikis.pdf). 이 값을 15:20 종가 단일가 거래량과 비교해 `last30`이나 종가 단일가 방향 종류의 피처로 쓴다. 엔진에는 ETF 순자산 자료가 없다. 운용사·KRX의 일별 AUM 수집이 필요하다. 삼성전기에는 단일종목 LETF가 없다(1차 추정). 미국 LETF 장중 momentum은 표본 밖에서 감쇠했다 — [QuantRocket](https://quantrocket.com/blog/leveraged-etf-intraday-momentum).
3. **TOM × 외국인 순매수**. 한국 TOM 효과에 외국인이 기여한다 — [Lee & Kim 2022](https://ideas.repec.org/a/eee/pacfin/v75y2022ics0927538x22001214.html). 엔진의 10년 일별 투자자 수급으로 일 단위 종류(`gap`, `open_to_close`)에서 시험할 수 있다.
4. **실적 발표 창**. 대상은 삼성전자 잠정실적, SK하이닉스·삼성전기 실적, Micron 실적이다. 발표 전 며칠은 premium 쪽(상승 기저율), 발표 기간은 단기 반전 강화 쪽이다 — [Frazzini & Lamont](https://www.nber.org/papers/w13090), [So & Wang](https://doi.org/10.2139/ssrn.2275982). 4분 모델에서는 이 창에서 VWAP 되돌림·반전 계열의 가중을 조건부로 키우는 실험이 된다. 일 단위에서는 플래그다.
5. **IBS·종가 위치**. 1분봉으로 비용 없이 계산된다. (종가 − 저가)/(고가 − 저가)를 다음날 `gap`·`open_to_close`의 피처로 넣는다. 장중에는 "오늘 범위 안의 현재 위치"가 4분 모델의 VWAP 이탈 피처와 겹치는지 먼저 확인한다. 근거는 C다 — [Pandey & Joshi 2023](https://arxiv.org/abs/2306.12434).
6. **일별 공매도 흐름**. 종목별 일별 공매도 비중을 1~5일 방향 피처로 쓴다 — [Diether, Lee, Werner 2009](https://ideas.repec.org/a/oup/rfinst/v22y2009i2p575-607.html). 2025-03-31 재개 뒤 이력만 쓸 수 있어 짧다(1차 노트 08). KRX의 공개 시점과 형식은 확인하지 못했다.
- **맞지 않는 것**: dispersion·VIX basis·delta-neutral(옵션 포지션이 필요하고 VKOSPI 선물 유동성 미확인), ETF 간 일중 arbitrage·index arbitrage(속도 경쟁. basis는 이미 피처), rebate trading, M&A, payday(미국 16일 급여), lunar cycle, January·Sell in May·대선 주기(horizon이 길고 표본이 적음), 횡단 decile 규칙(종목 셋).

### Inferences
- **캘린더 drift는 4분 방향에 거의 영향이 없다.** 예: Savor & Wilson의 발표일 초과수익 11.4bp를 하루 표준편차 2%짜리 종목에 그대로 옮긴다고 가정하자. 4분은 정규장 390분의 약 1.03%다. drift는 0.12bp, 표준편차는 약 20bp다. 상승 확률은 약 0.23%p만 바뀐다. 하루 단위로 보면 drift/표준편차 = 0.057이고, 상승 확률은 약 +2.3%p 바뀐다. 그래서 캘린더 계열은 일 단위 종류와 변동폭(HAR) 조건에 쓰고, 4분 방향에는 쓰지 않는다. 미국 크기를 그대로 옮긴다는 가정은 검증되지 않았다.
- **검정력**: TOM은 1년 12번, 10년 120번이다. 적중률 표준오차는 0.5/√120 ≈ 4.6%p다. FOMC는 10년 80번(≈ 5.6%p), 설날+추석은 10년 20번(≈ 11.2%p)이다. TOM만 10년 일봉으로 겨우 볼 수 있다. 나머지는 플래그와 층화 평가로만 쓰는 것이 정직하다.
- **하락 신호와의 관계**: 새 계열 대부분은 상승 drift다(TOM, 휴장 전, 발표일, 실적 premium, 만기 주). 하락 쪽을 주는 것은 LETF 리밸런싱(내린 날), 공매도 흐름, 실적 발표 전후 반전, IBS 상단뿐이다. 인버스 ETF로 하락을 거래하려면 이 넷이 후보다. 이때도 인버스 ETF 비용표(1차 권고 3번)가 먼저다.
- 2번(LETF 수요)은 1차가 "intraday momentum의 원인"으로만 다룬 것을 측정 가능한 수치로 바꾸는 일이다. 단일종목 LETF가 2026-05-27에 생겼으므로 이전 이력과 비교하는 자연실험이 된다. 1차 노트 04가 이 비교를 다룬 arXiv preprint를 찾았지만 결론은 확인하지 못했다.

### Gaps
- 국내 LETF 운용사가 리밸런싱을 현물 종가 단일가로 하는지, 선물·스왑으로 하는지 확인하지 못했다. 이것에 따라 2번 피처의 대상(현물 종가 대 선물)이 달라진다.
- 삼성전자 잠정실적 공시일의 규칙성(분기 첫 주 등)은 이번에 확인하지 않았다.
- 엔진 데이터에 ETF AUM, 일별 공매도, 거시지표 일정이 있는지 코드에서 확인하지 않았다.
