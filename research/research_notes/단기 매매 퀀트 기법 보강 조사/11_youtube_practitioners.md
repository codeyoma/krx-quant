# 유튜브·강연·팟캐스트의 실무 단기 매매 기법 (1차 조사 보강)

*기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다. 1차 보고서(`reports/단기 매매 퀀트 기법 조사.md`)와 노트 01~10에 이미 있는 것은 "1차 다룸"으로 표시하고 새로 나온 것만 자세히 적었다. 영상 날짜는 YouTube 페이지의 게시일(미국 태평양시)이다. 자막(transcript)은 YouTube가 내려주지 않아 읽지 못했다. 그래서 영상 내용은 제목과 설명란 기준이다. WebSearch는 15회를 썼고 끝까지 작동했다. 나머지는 WebFetch, YouTube 페이지 직접 조회, Crossref·OpenAlex 메타데이터로 확인했다.*

등급: **논문 있음**(동료심사 또는 그에 준하는 실증) / **엇갈림** / **근거 없음**. 신규 여부: **신규** / **부분 신규** / **1차 다룸**.

## 1. 유명 퀀트와 회사의 강연에서 나온 기법

### Takeaway
유명 퀀트의 공개 강연은 신호를 거의 알려 주지 않는다. 새로 건질 것은 세 가지다. Carver의 스캘핑 봇 실험은 비용과 체결 가정이 결과를 뒤집는다는 것을 보여 준다. Davey의 "monkey test"는 무작위 진입과 비교하는 검증 도구다. Joubert 등은 meta-labeling을 통제 실험으로 검증했다. Chan의 buy-on-gap과 Raschke의 setup은 독립 재현에서 엇갈리거나 근거가 없다.

### Cited Findings

| 기법 (1~2줄) | 발표자·채널 | 영상·글 (날짜) | 근거 | 등급 | 신규 |
|---|---|---|---|---|---|
| **Bracket 스캘핑 봇**: 현재가를 균형가로 보고 ±F·R/2(F = 0.75, R = 최근 H초 high-low)에 지정가 양방향 주문을 낸다. 한쪽이 체결되면 반대쪽을 취소하고 K·R/2에 stop을 둔다 | Robert Carver (qoppac 블로그) | [블로그](https://qoppac.blogspot.com/2025/05/can-i-build-scalping-bot-blogpost-with.html) (2025-05-05) | 저자 자신의 시뮬레이션과 실거래. 논문 없음 | 근거 없음 (실거래 실패) | 신규 |
| **Monkey test**: 전략의 청산 규칙은 두고 진입만 무작위로 바꿔 같은 거래 수로 수천 번 돌린다. 실제 전략이 그 분포를 넘는지 본다 | Kevin Davey(책 *Building Winning Algorithmic Trading Systems*). Better System Trader 팟캐스트 | [BST ep.113](https://www.youtube.com/watch?v=eMInEZH3Do4) (2017-08-10) | 검증 방법이다. 등급 대상이 아니다 | 해당 없음 | 신규 |
| **Buy-on-gap**: 전일 저가 대비 시가 하락폭이 90일 수익률 1σ보다 크고 시가가 20일 MA 위인 종목을 시가에 사서 종가에 판다 | Ernie Chan (책 *Algorithmic Trading*, 2013). Quantra 강의 | [Quantra](https://www.youtube.com/watch?v=NcI659Wmrb4) (2019-08-03), [QuantConnect gap fade 튜토리얼](https://www.youtube.com/watch?v=uf3TMjU8xWo) (2020-01-08) | 독립 백테스트가 부정적이다(아래) | 엇갈림 | 부분 신규 (1차는 시가 갭 반전만 다룸) |
| **Meta-labeling** 실증 보강 | Hudson & Thames, Marcos López de Prado | [H&T](https://www.youtube.com/watch?v=WbgglcXfEzA) (2021-02-06), [López de Prado 강연](https://www.youtube.com/watch?v=fOCzNFwmM7Y) (2024-12-17) | JFDS 논문 3편(아래) | 논문 있음 (통제 실험) | 1차 다룸, 증거 보강 |
| **Holy Grail / Turtle Soup / 80-20**: 강한 추세(ADX)에서 20 EMA 눌림 매수 / 20일 고가·저가의 가짜 돌파를 역매매 / 전날 range 한쪽 끝에서 열고 반대쪽 끝에서 닫은 날의 다음날 setup | Linda Raschke (Connors와 공저 *Street Smarts*, 1995~96) | [Chat With Traders EP 048](https://www.youtube.com/watch?v=l6fWeiwwjOI) (2016-04-26), [Prop Trader Fest](https://www.youtube.com/watch?v=owkPtpENiP0) (2026-10-07) | 규칙 자체를 검증한 논문은 찾지 못했다. 가짜 돌파 반전의 메커니즘은 Osler의 FX 연구가 일부 받친다(4절) | 근거 없음 (규칙) | 신규 |
| 공개 강연 (기법 없음) | Jim Simons | [Numberphile](https://www.youtube.com/watch?v=QNznD9hMEh0) (2015-05-13), [MIT 강연](https://www.youtube.com/watch?v=SVdTF4_QrTM) (2010-12-09 강연, 2013 업로드), [Abel Prize](https://www.youtube.com/watch?v=QUTaQvnwLzM) (2022-06-13) | — | — | 1차 다룸 |
| Avellaneda–Stoikov식 market making 설명 | Roman Paolucci (Quant Guild) | [영상](https://www.youtube.com/watch?v=aVzFKwyzwM0) (2025-12-02) | 주문을 내지 않는 엔진에는 해당 없음 | — | 1차 다룸 |

- **Carver 스캘핑 봇 수치**: 비용 0일 때 H = 120초, K = 0.8의 연율 SR은 약 48.6이다. 거래·취소당 약 $0.25 비용을 넣으면 28.8이다. 그런데 stop이 지정가가 아니라 다음 가격에 체결된다고 하면 같은 시스템이 약 **−120**이 된다. stop에 한쪽 slippage만 넣어도 7.0이다. H = 300초, K = 0.85는 비용 뒤 1.8이다. S&P 500 micro 선물에 H = 900초로 며칠 실거래한 뒤 그는 추기에 "It didn't work"라고 썼다 — [qoppac](https://qoppac.blogspot.com/2025/05/can-i-build-scalping-bot-blogpost-with.html)
- Carver의 다른 강연 "Trading Strategies that are Designed, Not Fitted"(QuantCon NYC 2017)는 fitting보다 설계를 강조한다. 단기 기법은 없다 — [Quantopian](https://www.youtube.com/watch?v=-aT55uRJI8Q) (2017-12-12)
- **Monkey test 출처**: BST ep.113은 진입과 청산의 효과를 따로 재는 방법을 다룬다. 진행자가 "breakout 전략에서 진입은 느슨해도 결과가 비슷했다"고 말한다 — [YouTube](https://www.youtube.com/watch?v=eMInEZH3Do4). Davey의 책을 인용한 포럼 글은 이렇게 쓴다. 청산은 두고 진입을 무작위로 바꾼다. 거래 수와 long/short 비율을 맞추고 여러 번 돌려 비교한다. Davey 원문은 확인하지 못했다 — [futures.io](https://futures.io/trading_member/40930-taterblue.html) [검색 요약]
- **Buy-on-gap 재현**: QuantRocket은 비슷한 규칙(상위 10% 거래대금, 시가가 전일 저가보다 1σ 이상 낮으면 개장 1분 뒤 매수, 종가 청산)을 시험했다. 결과가 너무 나빠 거꾸로 하라고 했다. gap-down 공매도는 2014~2020년 Sharpe 1.30, 연 약 10%였다. 비용 가정은 적혀 있지 않다. 이 글은 Chan을 인용하지 않는다 — [QuantRocket, Brian Stanley 2020-09-02](https://www.quantrocket.com/blog/buy-or-sell-down-gaps/)
  - Chan 책의 Euro STOXX 50 선물 결과(2004~2012, 연 13%, Sharpe 1.4)를 NinjaTrader로 2008~2013년에 재현하니 Sharpe 0 이하였다는 포럼 보고가 있다 — [futures.io](https://futures.io/trading_member/30155-zohar.html) [검색 요약]. 소형 가치주 백테스트에서도 성과가 시간이 갈수록 나빠졌다 — [financialnoob](https://financialnoob.substack.com/p/buy-on-gap-strategy-and-its-performance) [검색 요약]
- **Meta-labeling 논문**: Joubert(2022)는 meta-labeling을 세 요소로 나눴다. 통제 실험으로 각 요소가 Sharpe와 MDD를 어떻게 바꾸는지 보였다. 1차 모델의 false positive를 거르는 층이라는 정의다 — [Joubert, JFDS 4(3) 2022](https://doi.org/10.3905/jfds.2022.1.098). 후속으로 [Meyer, Joubert, Alfeus "Meta-Labeling Architecture" (JFDS 2022)](https://doi.org/10.3905/jfds.2022.1.108)와 [Thumm, Barucca, Joubert "Ensemble Meta-Labeling" (JFDS 2022)](https://doi.org/10.3905/jfds.2022.1.114)가 있다. 세 논문 모두 Hudson & Thames 사람이 썼다
- **Raschke 규칙**: Turtle Soup은 추세 지속을 기대하는 사람들이 걸리는 가짜 돌파를 노린다 — [turtletrader.com](https://www.turtletrader.com/trader-raschke/) [검색 요약]. Holy Grail의 "ADX + 20 EMA"는 [제3자 영상 제목](https://www.youtube.com/watch?v=e31xZ9O5V7Y)으로만 확인했다. 80-20의 세부 규칙은 출처마다 달라 확정하지 못했다. 2026-10-07 강연 설명란에 따르면 그녀는 swing high·low, ABC, trading range, false breakout, 세션 시작 시점을 설명한다 — [Prop Trader Edge](https://www.youtube.com/watch?v=owkPtpENiP0)
- Simons MIT 강연의 설명란은 시장 팁이나 매매 비밀을 기대하지 말라고 미리 적어 둔다 — [YouTube](https://www.youtube.com/watch?v=SVdTF4_QrTM)
- Euan Sinclair(2026-08-10 대담): 1998년에 돈을 찍어 내던 GARCH 모형은 이미 가격에 반영됐다고 본다. 지금 edge는 더 나은 가격 모형이 아니라 "상황을 찾는 것"이라고 말한다(설명란 기준) — [The Sophron Network](https://www.youtube.com/watch?v=9cLs5VvaYdM)
- Jane Street와 Optiver 공식 채널 영상은 채용, 문화, 기술 소개다. 신호는 없다 — [Jane Street mock interview](https://www.youtube.com/watch?v=NT_I1MjckaU), [Optiver](https://www.youtube.com/watch?v=IPkW3lYWaTk)

### Inferences
- Carver 실험의 교훈은 krx-quant의 채점에 바로 쓰인다. 닿음(touch) 손익을 "목표가에 정확히 체결"로 계산하면 크게 낙관적일 수 있다. 다음 틱이나 다음 호가 체결로 다시 재야 한다.
- Monkey test는 엔진의 기준 예측(시간대 기후 등)에 하나를 더하는 것과 같다. 같은 개수, 같은 시간대 분포로 무작위 시점에 낸 신호의 적중률 분포를 기준으로 삼는다.
- 회사 강연(Jane Street, Optiver, HRT)은 1차 결론(속도와 재고 관리로 번다)을 바꾸지 않는다.

### Gaps
- 자막을 못 받아 Simons, López de Prado, Raschke, Carver 강연의 발언을 직접 인용하지 못했다.
- Davey 책의 monkey test 원문 절차와 통과 기준은 확인하지 못했다.
- Chan 책의 buy-on-gap 원문 수치는 포럼 요약으로만 봤다.
- Ernie Chan, Andreas Clenow, Larry Connors, Kevin Davey의 최근(2025~2026) 강연은 따로 찾지 못했다. Clenow는 주로 일 단위 이상의 momentum이라 범위 밖이다.

## 2. 한국 유튜브 퀀트 채널의 단기 기법

### Takeaway
한국 개인 퀀트 유튜브에서 가장 많이 퍼진 단기 시스템은 Larry Williams의 **변동성 돌파**다. 강환국은 2019년부터 이를 소개했다. 그와 systrader79의 책 전략은 가상화폐 자동매매 앱으로도 나왔다. 학술 근거는 비용을 다루지 않은 약한 논문 하나뿐이다. 오히려 쓸모 있는 것은 한국 채널의 반례다. 2026년 AlgoLab 영상은 삼성전자 14년 데이터로 "승률 90%" 역추세 전략의 착시를 해체했다.

### Cited Findings

| 기법 | 발표자·채널 | 영상 (날짜) | 근거 | 등급 | 신규 |
|---|---|---|---|---|---|
| **변동성 돌파**: 당일 시가 + k × (전일 고가 − 전일 저가)를 넘으면 매수하고 다음날 시가에 청산한다. k는 0.5 근처이거나 "노이즈 비율"의 평균을 쓴다 | 강환국 (할 수 있다! 알고 투자) | [#53](https://www.youtube.com/watch?v=7jGzo3bv7jc) (2019-02-26), [#342](https://www.youtube.com/watch?v=SriNdCRLIVE) (2020-04-28) | S&P 500 단일 논문, 비용 미언급 | 근거 없음 | 신규 (1차의 noise-area·ORB와 같은 계열) |
| 같은 전략의 Pine Script 백테스트 | 장도강 | [영상](https://www.youtube.com/watch?v=7My1dT4nmNU) (2022-05-16) | 비동료심사 | 근거 없음 | 신규 |
| KOSPI200 선물 롱숏 변동성 돌파, 전진분석(walk-forward) | 와치독 자동매매 연구소 | [영상](https://www.youtube.com/watch?v=JTaI3bk5a_s) (2024-11-12) | 도구 홍보 영상 | 근거 없음 | 신규 |
| 변동성 돌파 + 손절 | 철투 | [영상](https://www.youtube.com/watch?v=hGfGJPnPoY8) (2019-12-09) | 일봉으로는 손절·익절을 제대로 시험할 수 없다고 경고 | — | 신규 |
| 이격도 기반 단기 트레이딩(systrader79 노하우 소개) | 강환국 | [#495](https://www.youtube.com/watch?v=WG7y7FE01tA) (2021-03-05) | 규칙 미확인 | 근거 없음 | 신규 |
| RSI PowerZones (승률 91% 주장) | 강환국, 출처는 Larry Connors *Buy the Fear, Sell the Greed* (2018) | [#563](https://www.youtube.com/watch?v=_az7zc-9UgM) (2021-07-27) | 1차의 RSI(2)·Connors와 같음 | 근거 없음 | 1차 다룸 |
| "장중에 반복적으로 사고팔지 마라" | 강환국 shorts | [영상](https://www.youtube.com/watch?v=RdyaP-EUHU8) (2023-09-19) | 1차의 비용·개인 day trader 결과와 일치 | — | 1차 다룸 |
| **"승률 90%" 역추세 전략 재검증** (Bollinger + RSI, 삼성전자 14년) | 알고랩 AlgoLab | [영상](https://www.youtube.com/watch?v=StTX9g8ZkF8) (2026-07-16) | 비동료심사지만 숫자를 공개 | (1차의 D 등급을 받침) | 신규 (한국 종목 반례) |
| 백테스트 함정 10가지 | 월가아재의 과학적 투자 | [영상](https://www.youtube.com/watch?v=BFkPzl1uziE) (2021-10-01) | 검증 원칙 | — | 1차 다룸 |

- 강환국 #53 설명란: 가장 존경하는 트레이더로 Larry Williams를 꼽는다. "매우 심플하지만 강력한" 변동성 돌파가 "한국에서도 잘 먹히는 전략"이라고 적었다. 그런데도 실전에서 버티기 어려운 이유를 다룬다고 했다 — [YouTube](https://www.youtube.com/watch?v=7jGzo3bv7jc)
- 강환국 #342 설명란: 2018년 systrader79와 쓴 가상화폐 책의 전략을 2019년 말까지 업데이트했다. 헤이비트는 이 책의 "변동성 돌파전략"으로 고객 자산을 가상화폐에 자동 투자하는 앱이라고 소개한다 — [YouTube](https://www.youtube.com/watch?v=SriNdCRLIVE)
- 규칙 설명: "당일 변동성 돌파선을 넘으면 매수하고 다음날 시가에 청산"한다. 비트코인 일봉용이다 — [TradingView 스크립트 (CHULTOO)](https://my.tradingview.com/script/HhBFmGo0) [검색 요약]. 노이즈 비율 K를 기간 평균으로 받는 변형도 있다 — [TradingView TVExtBot](https://vn.tradingview.com/script/Hl1P9sib) [검색 요약]
- 학술 근거: S&P 500에서 변동성 돌파가 buy-and-hold보다 "약간 높은" 수익을 냈다는 2023년 논문이 하나 있다. k는 "일반적 수준"으로 정했다. 거래비용과 slippage는 언급하지 않았다 — [홍성혁 2023, 사물인터넷융복합논문지 9(6):57-62](https://koreascience.or.kr/article/JAKO202301043221303.pub?lang=en)
- **AlgoLab 재검증**(설명란 수치):
  - 삼성전자 14년치에서 27개 설정 중 가장 좋은 것을 고르면 총수익 83%, 승률 71%, MDD 14%다.
  - 앞 구간으로 설정을 정해 처음 보는 뒤 구간에 적용하니 S&P 500 기준 총수익 29%·Sharpe 0.31이 8%·0.19로 줄었다.
  - 왕복 비용 0.2%면 83%가 75%로, 0.4%면 68%로, 0.6%면 61%로 줄었다.
  - 테슬라 승률 91%는 5종목 중 가장 좋은 것이었다(S&P 0.39, 삼성 0.58, 나스닥 0.66, 코스피 0.68, 테슬라 0.76)
  - 출처: [YouTube](https://www.youtube.com/watch?v=StTX9g8ZkF8)
- 강환국의 최근 영상 "1528. 돌파매매 하나면 개미탈출 확정!"(2025-10-01)은 설명란에 내용이 없어 규칙을 확인하지 못했다 — [YouTube](https://www.youtube.com/watch?v=gEn3ip6BXEk)

### Inferences
- 변동성 돌파는 1차의 Zarattini noise-area, ORB와 같은 "시가 대비 변동폭 경계 돌파" 계열이다. 1차 등급(C)보다 근거가 더 약하다. 다음날 시가 청산이라 overnight 위험까지 진다. krx-quant 기준으로는 새 알고리즘이 아니라 기존 noise-area kind 제안의 한 변형(k = 노이즈 비율)으로 묶으면 된다.
- AlgoLab의 삼성전자 수치는 1차 결론("승률은 설정 선택과 비용에 크게 흔들린다")을 한국 종목에서 다시 보여 준다. 사용자가 적중률로 판단하므로, 시도한 설정 수 N과 표본 밖 적중률을 함께 기록하는 규칙이 중요하다.
- RSI PowerZones 같은 "승률 90%" 규칙은 이익은 작게, 손실은 크게 두는 비대칭 손익에서 나온다. krx-quant의 대칭 ±k 닿음 정답에서는 그런 승률이 나오지 않는다. 비교하면 안 된다.

### Gaps
- systrader79 블로그의 단기 트레이딩 원문(영상 설명란이 지목한 103~109번 글)은 열지 못했다. 이격도 문턱과 보유 기간은 미확인이다.
- 조코딩 영상(예: [하락장에서도 수익나는 비트코인 자동매매](https://www.youtube.com/watch?v=5vofEMqMyGk))이 변동성 돌파를 쓰는지는 자막이 없어 확인하지 못했다.
- KOSPI200 선물이나 한국 개별주에서 변동성 돌파를 비용을 넣어 검증한 학술 논문은 찾지 못했다.
- 다른 한국 채널(퀀트대디, 퀀트 레시피 등)의 단기 시스템은 조사 한도 때문에 보지 못했다.

## 3. 실무자 팟캐스트 (Chat With Traders, Flirting with Models, Odd Lots, Cem Karsan 출연분)

### Takeaway
팟캐스트에서 새로 나온 단기 기법은 사실상 하나다. 옵션 딜러 포지션(gamma, vanna, charm)이 만기 주간의 장중 움직임을 바꾼다는 주장이다(Cem Karsan, SpotGamma). 이 주장의 "변동성과 자기상관" 부분은 논문이 받친다(4절). "flow 방향 예측" 부분은 근거가 없다. Odd Lots의 HFT 회차는 1차 결론(속도 경쟁)을 확인할 뿐이다.

### Cited Findings
- Cem Karsan(Kai Volatility)은 큰 분기 만기로 가는 주간이 한 달 중 변동성을 팔기 가장 좋은 주라고 주장한다. 미결제약정과 딜러 포지션이 커서 시장이 단기적으로 출렁이지만 "strongly mean reverting"이고, 큰 움직임은 이어지기보다 사그라든다고 본다. charm과 vanna flow가 만기로 갈수록 내재변동성을 누른다고 설명한다(설명란 기준) — [tastylive](https://www.youtube.com/watch?v=o8NMI3jpu3A) (2026-09-11); 더 오래된 설명: [RCM Alternatives "Vol Curves and Vanna Charm"](https://www.youtube.com/watch?v=8awiGrquYXI) (2020-10-15)
- SpotGamma 창업자 Brent Kochuba는 옵션 포지션에서 계산한 "SpotGamma levels"가 무엇이고 어떻게 쓰는지 소개한다 — [TrendSpider](https://www.youtube.com/watch?v=Tu8eX3RASLo) (2024-06-20)
- GEX의 원래 정의는 SqueezeMetrics 백서 "Gamma Exposure (GEX)"(2016-03, 2017-12 개정)다. 제목과 날짜만 확인했다. 본문은 글꼴 인코딩 때문에 읽지 못했다 — [PDF](https://squeezemetrics.com/download/white_paper.pdf). 3자 요약에 따르면 계약별 GEX = 미결제약정 × gamma × 100 × (콜 +1, 풋 −1)이다. 딜러가 콜은 사고 풋은 팔았다는 단순 가정을 쓴다 — [Slope of Hope 요약](https://slopeofhope.com/?p=187436) [검색 요약]
- Chat With Traders EP 274(Gary Norden): pit 출신 트레이더가 order flow 매매를 "lost art"로 소개한다. 위험 회피적 태도와 주당 수백 번의 order flow 매매를 말한다(설명란) — [YouTube](https://www.youtube.com/watch?v=7xRfeYvK_hE) (2024-01-23)
- Chat With Traders EP 048(Linda Raschke): 자기 틀(framework)로 매매하고, 모델로 가격 행태를 연구하는 이야기다 — [YouTube](https://www.youtube.com/watch?v=l6fWeiwwjOI) (2016-04-26)
- Odd Lots: HRT AI 연구 책임자 Iain Dunning이 AI 도구를 실제로 어떻게 쓰는지와 전통적 ML과 무엇이 다른지 말한다 — [YouTube](https://www.youtube.com/watch?v=ADfpBrl8Avo) (2025-10-31). 사회학자 Donald MacKenzie는 거래소 옆으로 서버를 붙이는 속도 경쟁의 역사를 말한다 — [YouTube](https://www.youtube.com/watch?v=NRLNO_KOh9I) (2026-03-02)
- Flirting with Models 홈페이지에서는 최근 4회만 보였다. 그중 단기 매매와 가까운 것은 없다. S7E32 Ben Wellington "Complex Feature Engineering at Two Sigma"(2026-07-06)가 피처 설계 쪽이다. 대본 제공 여부는 페이지에 없다 — [flirtingwithmodels.com](https://www.flirtingwithmodels.com/)
- Top Traders Unplugged의 Carver 회차는 추세추종의 단순성을 다룬다. 단기 기법은 아니다 — [YouTube](https://www.youtube.com/watch?v=klwnw3iSRuM)

### Inferences
- 딜러 gamma 주장 중 엔진에 쓸 수 있는 부분은 "만기 주간이나 양(+) gamma 국면에서는 4분 움직임이 되돌림 쪽으로 기운다"는 조건부 가설이다. 1차에서 Gauss(VWAP OU)가 κ = 0을 배운 것은 무조건부라서일 수 있다. gamma 국면 플래그로 나누면 다르게 나올 수 있다.
- vanna·charm으로 방향을 맞힌다는 주장은 검증 자료를 찾지 못했다. 엔진에 넣는다면 피처 후보이지 규칙이 아니다.

### Gaps
- Flirting with Models의 Cem Karsan, Euan Sinclair, Benn Eifert 회차와 대본은 찾지 못했다(검색 결과 없음, 홈페이지는 최근 4회만 노출).
- Chat With Traders, Odd Lots 대본은 받지 못했다. 위 내용은 설명란 기준이다.
- Top Traders Unplugged의 단기·장중 회차는 찾지 않았다(대부분 추세추종).

## 4. 실무 장중·스캘핑 기법과 그 근거

### Takeaway
실무자가 가르치는 장중 도구 대부분(market profile·TPO, volume profile, VWAP band, anchored VWAP, delta divergence, ICT/SMC)은 직접 검증한 논문이 없다. ICT/SMC는 2026년 체계적 평가에서 가르치는 그대로는 작동하지 않았다. 학술 근거가 있는 것은 그 밑에 깔린 세 메커니즘이다. (1) 라운드 넘버와 지지·저항에 지정가 주문이 몰리고 stop이 그 너머에 몰린다. (2) 딜러 gamma와 레버리지 ETF 리밸런싱이 장중 자기상관과 마감 직전 흐름을 바꾼다. (3) 거래량이 비정상적으로 많은 종목은 이후 오른다(일·주 단위).

### Cited Findings

**기법 표**

| 기법 (1~2줄) | 대표 발표자·영상 (날짜) | 근거 | 등급 | 신규 |
|---|---|---|---|---|
| **Footprint, cumulative delta, delta divergence**: 체결을 매수 공격·매도 공격으로 나눠 누적한다. 가격이 신고가인데 delta가 따라오지 않으면(divergence) 반전을 노린다 | Axia Futures: [delta reversal](https://www.youtube.com/watch?v=lYfUeEX7L3o) (2019-04-01), [cumulative delta](https://www.youtube.com/watch?v=s01_dNVCaNA) (2021-11-10), [continuation vs reversal](https://www.youtube.com/watch?v=CGJiqAPo7sg) (2022-03-27). Jigsaw Trading [order flow](https://www.youtube.com/watch?v=gdQNAMTDHfM) | delta 자체는 1차의 OFI·trade sign과 같다(동시 설명은 강하고 예측은 약함). divergence 규칙 검증은 없다 | 근거 없음 (divergence) | 부분 신규 |
| **Market profile (TPO), value area, POC**: 30분 단위 가격 분포로 하루의 "가치 영역"(거래 70% 구간)과 최빈가(POC)를 잡는다. 가치 영역 밖에서 받아들여지는지로 추세와 회귀를 가른다 | James Dalton: [Trading Value versus Price](https://www.youtube.com/watch?v=LgIPFsIjyLs) (2014-11-14), [Scalping with the Market Profile](https://www.youtube.com/watch?v=HeWdnIt5tGk) (2018-03-03) | 검색에서 실증 논문 없이 벤더 문서만 나왔다 | 근거 없음 | 신규 |
| **Volume profile (HVN/LVN)**: 가격대별 거래량이 많은 곳은 지지·저항, 적은 곳은 빠르게 통과한다고 본다 | CQG 등 벤더 | 직접 검증은 없다. 간접 근거(지지·저항과 호가 깊이)는 아래 | 근거 없음 (직접) | 신규 |
| **Anchored VWAP**: 실적, 갭, 신저가 같은 사건 시점부터 VWAP을 다시 계산해 그 이후 매수자·매도자의 평균 단가를 레벨로 쓴다 | Brian Shannon: [When, Where, Why to Set Anchored VWAP](https://www.youtube.com/watch?v=ZKM61HfY5fY) (2024-01-11), 책 *Maximum Trading Gains With Anchored VWAP* | 검색에서 실증 논문 없음. 1차는 일반 VWAP 회귀를 D로 판정 | 근거 없음 | 신규 |
| **Stocks in play, RVOL 필터** | SMB Capital: [Does trading Stocks In Play create automatic edge?](https://www.youtube.com/watch?v=6sVjKkPHlTE) (2018-05-10) | 장중은 Zarattini 계열(1차 C). 일·주 단위 고거래량 프리미엄은 JF 논문 | 논문 있음 (일 단위) / 엇갈림 (장중) | 부분 신규 |
| **Opening drive, 세션 시작** | Dalton, Raschke(2026 강연) | 1차의 첫 30분 momentum, 한국 09:00~09:10 반전 | (1차) | 1차 다룸 |
| **GEX 레벨, gamma flip**: 딜러 순 gamma가 양이면 움직임이 눌리고 행사가에 붙는다. 음이면 증폭된다. flip 가격을 레벨로 쓴다 | SpotGamma, SqueezeMetrics, Cem Karsan(3절) | 국면 조건으로서는 RFS 논문과 working paper가 있다. "flip 가격에서 반전" 같은 레벨 규칙은 근거 없음 | 논문 있음 (국면) / 근거 없음 (레벨) | 부분 신규 |
| **레버리지 ETF 마감 리밸런싱** | 업계 해설(미래에셋, 삼성증권) | 미국은 working paper 있음, 한국은 업계 주장 | 논문 있음 (미국) / 근거 없음 (한국) | 신규 (메커니즘) |
| **ICT / Smart Money Concepts**: fair value gap(3봉 사이 빈칸)은 되메워지고, liquidity sweep(고점·저점 너머 stop 사냥) 뒤에는 반전하며, order block에서 반등한다고 본다 | 유튜브 다수 | 2026 SSRN 체계적 평가에서 가르치는 그대로는 작동하지 않음 | 근거 없음 (반박 쪽) | 신규 |
| **지지·저항, 라운드 넘버, stop cascade** (위 기법들의 공통 토대) | — | FX와 미국 주식 논문 다수 | 논문 있음 | 신규 |

**근거 상세**

- *지지·저항과 라운드 넘버*
  - Osler(2003, JF)는 한 은행의 개별 FX stop-loss·take-profit 주문을 처음으로 분석했다. take-profit은 라운드 넘버에 강하게 몰리고, stop-loss는 라운드 넘버 바로 너머에 몰렸다. 앞의 것은 "지지·저항에서 반전한다"를, 뒤의 것은 "돌파 뒤 빨라진다"를 설명할 수 있다고 했다 — [DOI](https://doi.org/10.1111/1540-6261.00588)
  - Osler(2005, JIMF): stop-loss가 몰린 레벨에 닿으면 추세가 유난히 빨라지고 stop이 연쇄로 발동한다. stop-loss에 대한 반응이 take-profit보다 크고 오래간다. 효과는 "몇 시간은 유의하지만 며칠은 아니다" — [NY Fed Staff Report 150](https://www.newyorkfed.org/research/staff_reports/sr150.html)
  - Osler(2000): 6개 회사가 공표한 FX 지지·저항 레벨에서 1분 데이터 기준 반등률은 60.8%였다. 무작위 레벨은 56.2%였다(2차 요약 수치, 원문 PDF는 해독하지 못함) — [FRBNY EPR](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf), 수치 출처 [Thor 블로그](https://thortradecopier.com/blog/does-ict-smart-money-concepts-work)
  - Kavajecz & Odders-White(2004, RFS): 지지·저항 레벨은 지정가 호가창의 깊이(depth) 봉우리와 겹친다. 이동평균 신호는 호가창 깊이의 상대 위치를 드러낸다. 저자들은 기술적 규칙이 이미 있는 깊이를 찾아낼 뿐이라고 결론지었다 — [DOI](https://doi.org/10.1093/rfs/hhg057)
  - Bhattacharya, Holden, Jacobsen(2012, Management Science): 미국 주식 체결 1억 건 이상을 봤다. 유동성 수요자는 라운드 넘버 1센트 아래에서 더 사고 1센트 위에서 더 팔았다. 불균형 크기는 라운드 정도(정수 > 50센트 > …)에 비례했다. 호가가 정수까지 "내려와 닿을 때"가 "건너갈 때"보다 훨씬 강했다. 24시간 수익률도 가격대별로 달랐다 — [DOI](https://doi.org/10.1287/mnsc.1110.1364)
  - Garzarelli, Cristelli, Pompa, Zaccaria, Pietronero(2014, Scientific Reports): 지지·저항 후보를 찾는 기준을 만들었다. 가격은 그 값을 건너기보다 되튈 가능성이 더 높았다. 저자들은 이를 자기실현적 예언의 정량 증거로 읽었다 — [DOI](https://doi.org/10.1038/srep04487), [arXiv 1110.5197](https://arxiv.org/abs/1110.5197)
- *ICT/SMC 평가*
  - Mahadzva(2026, SSRN, 동료심사 전) "Smart Money or Costly Folklore?": G10 FX 장중(EURUSD, GBPUSD, AUDUSD, USDJPY)에서 코드화한 규칙 6개를 시험했다. "가르치는 그대로 작동한다는 증거는 없다." liquidity sweep 규칙은 교과서의 반전이 아니라 **지속** 쪽이었다(결합 fold 선택 66번 중 65번). 가장 좋은 결과(EURUSD 1시간, 거꾸로 매매, holdout 60거래 +19.3%, p = 0.0192)도 Deflated Sharpe Ratio를 통과하지 못했다. 제목과 연도는 [OpenAlex/DOI](https://doi.org/10.2139/ssrn.7430998)로 확인했다. 수치는 [Thor 블로그 요약](https://thortradecopier.com/blog/does-ict-smart-money-concepts-work) 기준이다(SSRN 원문은 403으로 막힘)
  - 같은 블로그의 random walk 시뮬레이션(발행사 Phoenix Technologies 자체, 외부 재현 없음): 100만 봉에서 bullish FVG가 20봉 안에 되돌아온 비율은 83.4%였다. 같은 거리의 무작위 레벨은 83.5%였다. 두 seed에서 최대 차이는 0.12%p였다 — [Thor 블로그](https://thortradecopier.com/blog/does-ict-smart-money-concepts-work)
- *딜러 gamma*
  - Ni, Pearson, Poteshman, White(2021, RFS 34(4):1952–1986) "Does Option Trading Have a Pervasive Impact on Underlying Stock Prices?" — [DOI](https://doi.org/10.1093/rfs/hhaa082). 2차 요약: 옵션 헤저의 순 gamma는 주식 변동성과 음의 관계다. 헤지 리밸런싱은 옵션이 있는 종목의 일간 절대수익률 가운데 약 9.5%(1990~2001)와 13.4%(2002~2012)를 설명한다 — [BSIC 2022-03-06](https://bsic.it/how-dealers-gamma-impacts-underlying-stocks/). 원문 초록은 직접 보지 못했다
  - Barbon & Buraschi "Gamma Fragility"(working paper, 2021-03 개정): 음(−)의 사전 gamma 불균형은 장중 momentum, 양(+)은 장중 반전과 연결된다. 비유동 종목일수록 강하다. gamma 불균형은 flash crash 빈도·크기와도 관련된다 — [저자 페이지](https://abarbon.com/papers/gamma-fragility), [SSRN 3725454](https://doi.org/10.2139/ssrn.3725454)
    - BSIC 요약 수치: gamma 불균형 1σ는 개별주 일간 절대수익률 5~25bp 감소(지수는 20bp 이상), 5분~60분 자기상관 1.5~2% 감소, 일중 고저폭 약 16bp 변화와 연관된다 — [BSIC](https://bsic.it/how-dealers-gamma-impacts-underlying-stocks/)
  - 마감 직전: 레버리지 ETF 리밸런싱과 옵션 delta 헤지가 개별주에 마감 momentum과 평균회귀를 만든다. 효과는 다음 날 사라진다. gamma 효과는 표본 내내 남았지만 레버리지 ETF 효과는 시간이 갈수록 줄었다. 레버리지 ETF flow는 예측하기 쉬워 유동성 공급이 더 붙고 가격 효과가 짧다 — [Barbon, Beckmeyer, Buraschi, Moerke, SSRN 3925725](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3925725) (저자 페이지 제목 "Liquidity Provision to Leveraged ETFs and Equity Options Rebalancing Flows", Crossref 제목 "The Role of Leveraged ETFs and Option Market Imbalances on End-of-Day Price Dynamics"; 요약은 [abarbon.com](https://abarbon.com/papers) 기준)
    - BSIC가 인용한 Beckmeyer & Moerke(2021): 마감 30분 전까지 기초자산이 1%p 움직일 때 delta 헤지가 약 18bp의 momentum이나 반전을 만든다. 방향은 시장조성자 총 gamma의 부호가 정한다. 대형주에서 더 강하다 — [BSIC](https://bsic.it/how-dealers-gamma-impacts-underlying-stocks/) [2차 요약]
  - 한국: 2026-05-27 삼성전자·SK하이닉스 단일종목 레버리지 ETF가 상장됐다. 미래에셋 집계로 14종이 7거래일(5/27~6/5) 동안 거래대금 58조원, 개인 순매수 7.4조원이었다. 미래에셋 윤재홍 연구원은 이 상품의 헤지가 하락장에서 short gamma 효과를 내 낙폭을 키웠다고 봤다. 삼성증권 이종욱은 상품이 커질수록 gamma 헤지 물량도 는다고 했다. VKOSPI는 6/9 91.23으로 2009년 공표 이래 최고였다. 이는 업계 견해이고 측정치는 아니다 — [아시아경제 2026-06-10](https://view.asiae.co.kr/en/article/2026061008155263219)
- *거래량*: 하루나 일주일 동안 거래량이 비정상적으로 많은(적은) 종목은 이후 한 달 동안 오르는(내리는) 경향이 있다. 저자들은 주목도(visibility) 효과로 해석했고 자기상관, 공시, 시장위험, 유동성으로는 설명되지 않았다 — [Gervais, Kaniel, Mingelgrin 2001, JF 56(3)](https://doi.org/10.1111/0022-1082.00349)
- Market profile 검색 결과는 CQG 문서 같은 정의뿐이었다. value area 문턱도 버전에 따라 거래의 70%와 68%로 달랐다 — [CQG 2026 primer](https://news.cqg.com/workspaces/2026/06/cqg-primer-market-profile-value-areas-mpva)
- VWAP 회귀와 anchored VWAP 검색 결과는 TradingView 스크립트와 교육 페이지뿐이었다. "VWAP에서 멀어지면 되돌아온다"는 관행 주장이었다 — [TradingView 교육 글](https://pl.tradingview.com/chart/ES1%21/BvjbdcMq-Understanding-VWAP-In-Trading) [검색 요약]

### Inferences
- 실무 레벨 도구(POC, value area, AVWAP, FVG)는 "많은 사람이 보는 가격"이라는 점에서만 의미가 있을 가능성이 크다. Kavajecz & Odders-White와 Bhattacharya 등의 결과가 그 해석을 받친다. 레벨이 힘을 갖는 이유는 그 가격에 실제 지정가와 stop 주문이 몰리기 때문이다. 그렇다면 krx-quant는 레벨을 추측할 필요가 없다. 10단계 호가창의 실제 depth를 이미 본다.
- Osler의 "닿으면 반전, 건너면 가속"은 ±k틱 닿음 예측과 구조가 같다. 4분 안에 라운드 넘버나 전일 고가·저가가 목표 사이에 있는지는 닿음 확률을 바꿀 수 있는 조건이다.
- ICT의 liquidity sweep이 실제로는 "지속"이었다는 결과는 Osler의 stop cascade(건너면 가속)와 맞는다. Turtle Soup류의 "가짜 돌파 역매매"는 평균적으로 반대 방향에 서는 셈일 수 있다.
- 딜러 gamma와 레버리지 ETF 리밸런싱은 1차에서 "마지막 30분 momentum의 원인"으로만 다뤘다. 새로 확인한 것은 두 가지다. 같은 메커니즘이 개별주의 5~60분 자기상관 부호까지 바꾼다(Barbon & Buraschi). 그리고 2026-05-27부터 정확히 삼성전자·SK하이닉스에 단일종목 레버리지 ETF라는 새 리밸런싱 수요가 생겼다.

### Gaps
- KOSPI200 옵션 딜러 gamma나 한국 레버리지 ETF 리밸런싱의 장중 효과를 잰 학술 논문은 찾지 못했다(검색 1회).
- Ni 등(2021) 원문 초록과 Mahadzva(2026) 원문은 직접 읽지 못했다. 수치는 2차 요약이다.
- Stivers & Sun(2013, JBF) "Returns and option activity over the option-expiration week for S&P 100 stocks"는 존재만 확인했다. 결과는 미확인이다 — [DOI](https://doi.org/10.1016/j.jbankfin.2013.07.030)
- "Volatility as a Signal Switch: Regime-dependent Intraday Mean Reversion in Nasdaq-100 Futures"(Seeck 2026, SSRN 7364204)는 제목만 확인했다 — [DOI](https://doi.org/10.2139/ssrn.7364204)
- tape reading(체결창 읽기)을 정량화한 논문은 따로 찾지 않았다. 1차의 OFI·trade sign 결과로 대신한다.
- 1차에서 "0DTE gamma risk"로 언급한 Dim, Eraker, Vilkov(2024)의 결과는 이번에도 확인하지 못했다 — [DOI](https://doi.org/10.2139/ssrn.4692190)

## 5. krx-quant 적용 메모

### Takeaway
새 기법 가운데 엔진 데이터로 바로 시험할 만한 것은 레벨 근접 피처(라운드 넘버, 전일 고가·저가·VWAP), gamma·레버리지 ETF 국면 플래그, 무작위 진입 기준선 세 가지다. Market profile, AVWAP, ICT, delta divergence는 규칙으로 넣지 말고, 넣더라도 LightGBM 피처 후보로만 두는 편이 근거와 맞는다. 모두 조사에서 나온 가설이며 엔진에서 검증하지 않았다.

### Cited Findings
- 라운드 넘버 1틱 아래 매수 초과, 위 매도 초과. 닿을 때가 건널 때보다 강하다 — [Bhattacharya et al. 2012](https://doi.org/10.1287/mnsc.1110.1364). take-profit은 라운드 넘버에, stop-loss는 그 너머에 몰린다 — [Osler 2003](https://doi.org/10.1111/1540-6261.00588). stop 연쇄 효과는 몇 시간 유지된다 — [Osler 2005](https://www.newyorkfed.org/research/staff_reports/sr150.html)
- 지지·저항은 호가창 depth 봉우리와 겹친다 — [Kavajecz & Odders-White 2004](https://doi.org/10.1093/rfs/hhg057)
- gamma 불균형의 부호가 장중 자기상관(momentum과 반전)의 부호를 가른다 — [Barbon & Buraschi](https://abarbon.com/papers/gamma-fragility). 레버리지 ETF와 옵션 리밸런싱이 마감 흐름을 만들고 다음 날 사라진다 — [SSRN 3925725](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3925725)
- 삼성전자·SK하이닉스 단일종목 레버리지 ETF는 2026-05-27 상장했다. 업계는 short gamma 효과를 주장한다 — [아시아경제](https://view.asiae.co.kr/en/article/2026061008155263219)
- stop 체결을 지정가로 가정하느냐 다음 가격으로 가정하느냐에 따라 같은 스캘핑 시스템의 SR이 +28.9에서 −120으로 바뀌었다 — [Carver 2025](https://qoppac.blogspot.com/2025/05/can-i-build-scalping-bot-blogpost-with.html)
- 삼성전자 Bollinger+RSI 역추세에서 27개 설정 중 최선을 고르면 승률 71%가 나왔다. 표본 밖과 비용을 넣으면 성과가 크게 줄었다 — [AlgoLab 2026-07-16](https://www.youtube.com/watch?v=StTX9g8ZkF8)
- ICT liquidity sweep은 반전이 아니라 지속 쪽이었다 — [Mahadzva 2026](https://doi.org/10.2139/ssrn.7430998) ([요약](https://thortradecopier.com/blog/does-ict-smart-money-concepts-work))

### Inferences
우선순위는 비용 대비 기대 효과 순이다. 모두 1차 보고서의 제안(채점 보강, gap, open_to_close, last30, horizon 사다리)을 바꾸지 않고 그 위에 더하는 것이다.

| 순서 | 무엇 | 어디에 | 데이터 | 시험 방법 |
|---|---|---|---|---|
| 1 | **무작위 진입 기준선(monkey test)**: 각 알고리즘의 신호와 같은 개수, 같은 시간대 분포, 같은 방향 비율로 무작위 시점 신호를 수천 번 만들어 적중률 분포를 구한다. 실제 적중률의 분위수를 기록한다 | 리플레이 채점(기존 기준 예측 4개 옆) | 있음 | 분위수 95% 이상만 통과 |
| 2 | **체결 가정 민감도**: 닿음 손익을 "목표가 체결"과 "닿은 뒤 다음 체결가 또는 반대 호가 체결" 두 가지로 계산한다 | 닿음 손익 | 체결·호가 있음 | 두 값의 차이를 종목별로 공개 |
| 3 | **레벨 근접 피처**: (a) 가장 가까운 라운드 가격(예: 삼성전자 1만원 단위, SK하이닉스 10만원 단위. 단위는 데이터로 정함)까지의 틱 거리와 부호 (b) 전일 고가·저가·종가, 당일 시가, 전일 VWAP까지의 틱 거리 (c) "±k틱 목표 사이에 레벨이 있는가"와 "직전 N분에 레벨을 건넜는가" 플래그 | Turing, Breiman(LightGBM), Shannon | 체결, 1분봉 있음 | 레벨 앞에서 닿음 확률이 줄고, 건넌 직후 같은 방향 닿음이 느는지(Osler) 본다 |
| 4 | **depth 봉우리 피처**: 10단계 호가에서 평균 대비 depth가 튀는 가격까지의 거리. 레벨 추측(POC, AVWAP) 대신 실제 대기 주문을 쓴다 | Shannon, Kalman | 10단계 호가 있음 | 3과 같은 방식. 3과 겹치면 하나만 남긴다 |
| 5 | **레버리지 ETF 리밸런싱 추정**: 종목별 Σ AUMᵢ × Lᵢ(Lᵢ − 1) × 당일 기초자산 수익률(L = 2면 계수 2, L = −2면 6). 마감 전 필요한 같은 방향 매매 규모를 원화로 추정하고 거래대금으로 정규화한다. 이 계수는 일일 재설정 구조에서 나온 산술이고 논문 수치가 아니다 | 1차 보고서의 `last30` kind, 마감 구간 4분 예측의 조건 | **새 데이터**: 단일종목 레버리지·인버스 ETF 일별 AUM(KRX ETF 공시) | 2026-05-27 전후 마지막 30분 수익률과 당일 수익률의 관계가 바뀌었는지 이벤트 연구로 본다. 표본은 아직 수개월뿐이다 |
| 6 | **gamma 국면 플래그**: 만기 주간, 만기일, (가능하면) KOSPI200 옵션 미결제약정으로 계산한 딜러 순 gamma 부호 추정 | Gauss(되돌림), Newton(momentum)의 조건 변수. Markov 국면 입력 | ATM 옵션 IV는 있다. **미결제약정 전체는 새 데이터** | 국면별로 4분 적중률과 직전 수익률의 자기상관 부호를 나눠 본다. 1차의 Gauss κ = 0이 국면을 섞은 탓인지 확인한다 |
| 7 | **상대 거래량(RVOL)**: 같은 시각 기준 20일 평균 대비 누적 거래량. 단독 신호가 아니라 Newton·Gauss의 국면 조건으로 쓴다 | 피처, 게이트 | 1분봉 1년 있음 | 고RVOL 날의 momentum, 저RVOL 날의 되돌림을 본다 |
| 8 | (낮음) **delta divergence 플래그**: 4분 창에서 가격은 신고가인데 누적 체결 delta는 신고가가 아닐 때 | Shannon 피처 | 체결 있음 | OFI가 이미 담고 있을 가능성이 크다. 추가 lift가 없으면 버린다 |
| 9 | (낮음) **변동성 돌파 kind**: 당일 시가 + k × 전일 range(k = 최근 n일 노이즈 비율 평균)에 닿은 뒤 종가까지 그 방향이 유지되는가. 1차의 noise-area kind와 하나로 합친다 | 일 단위 예측 종류 | 1분봉 | 하루 1표본이라 1년으로는 판정이 어렵다(1차의 표준오차 메모) |

- 넣지 않을 것: market profile(TPO)·value area·POC, anchored VWAP, ICT(FVG, order block, sweep 반전)를 **규칙**으로 넣는 것. 직접 근거가 없고 ICT는 반박 쪽 결과가 있다. 레벨의 정보는 3, 4번 피처가 더 직접 담는다.
- 하락 신호가 인버스 ETF로 실행된다는 점에서 5번은 두 방향으로 중요하다. 단일종목 인버스(−2배)의 리밸런싱 계수가 6으로 가장 크다. 기초자산이 오른 날에는 마감 매수 수요를 더한다. 하락 신호를 인버스로 실행하는 사용자는 그 ETF 자체의 마감 흐름에 노출된다.
- 사용자는 적중률로 판단한다. 그러니 1번(monkey test)과 2번(체결 가정)을 먼저 해야 이후 피처의 lift가 진짜인지 가릴 수 있다. 시도한 피처·설정 수도 1차 보고서의 다중검정 누적에 더한다.

### Gaps
- KRX에서 라운드 넘버 효과를 측정한 논문은 찾지 못했다. 호가단위 경계(예: 20만원, 50만원) 근처의 주문 행태도 미확인이다.
- 단일종목 레버리지·인버스 ETF의 일별 AUM을 엔진이 얻을 수 있는지(KIS API나 KRX 공시 다운로드)는 확인하지 않았다.
- KOSPI200 옵션 전체 행사가의 미결제약정을 KIS 실시간이나 일별로 받을 수 있는지 확인하지 않았다. 없으면 6번은 만기 달력 플래그로만 시작해야 한다.
- 삼성전기(009150)에는 단일종목 레버리지 ETF가 없다고 1차 노트 08이 추정했다. 5번은 삼성전자와 SK하이닉스에만 해당한다.
