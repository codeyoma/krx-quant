# 유명 퀀트 트레이딩 회사와 공개된 기법 (2026-10-17 기준)

> 연구 자료일 뿐 투자 조언이 아니다. 수익률 수치는 대부분 회사가 공개하지 않은 것을 언론이 익명 소식통을 통해 보도한 것이다. 각 수치에 기준 연도와 출처를 붙였다. 확인하지 못한 것은 "미확인", 전해 들은 것은 "2차 보도"로 표시했다.

## 1. 회사 프로필 요약 표

### Takeaway
수익이 큰 곳은 두 부류다. 하나는 속도와 인프라로 버는 HFT/마켓메이커(Jane Street, Citadel Securities, HRT, XTX, Optiver), 다른 하나는 수많은 PM과 신호를 엄격한 위험 한도로 묶는 multi-strategy 펀드(Citadel, Millennium, D. E. Shaw)다. 혼자 하는 퀀트가 따라 할 수 있는 것은 "많은 약한 신호를 결합하는 것"과 "규율"이지 속도가 아니다.

### Cited Findings

| 회사 | 유형 | 호흡 | 대표 공개 수치 (기준 연도, 출처) | 혼자 하는 퀀트에게 주는 교훈 |
|---|---|---|---|---|
| Renaissance (Medallion) | 직원 전용 stat-arb 펀드 | 반나절~1.5주 ([책 요약](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)) | 1988–2021 연평균 세전 66%, 세후 39%. 2020년 +76% ([Institutional Investor, 2024-11](https://www.institutionalinvestor.com/article/2e0uykr3vn5booz0smrcw/hedge-funds/renaissances-2024-rebirth)) | 약한 신호를 많이 쓰고, 데이터를 정제하고, 모델은 하나로 통합한다 |
| D. E. Shaw | multi-strategy (퀀트+재량) | 다양 | Composite 2024년 +18%, 2025년 +18.5%. Oculus 2024년 +36.1%, 2025년 +28.2% ([II 2024](https://www.institutionalinvestor.com/article/2eaxu6g8f1zzvc4ipdc74/hedge-funds/d-e-shaw-tops-a-2024-hedge-fund-ranking), [Fortune 2026-01-02](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | 퀀트와 재량을 섞어 분산한다 |
| Two Sigma | 퀀트 multi-strategy | 다양 | Absolute Return Enhanced 2023년 +12%, Macro Compass Enhanced 2023년 −16% ([Bloomberg 2024-01-29](https://www.bloomberg.com/news/articles/2024-01-29/two-sigma-s-macro-compass-enhanced-fund-sank-16-last-year)). 2026년 3월 말 누적 +3%/+3.7% ([Bloomberg 2026-04-06](https://www.bloomberg.com/news/articles/2026-04-06/two-sigma-profits-from-chaotic-march-beating-multistrat-peers)). 2025년 연간 미확인 | 모델 변경 거버넌스 |
| Citadel (Wellington) | multi-manager pod | 다양 | 2024년 +15.1% ([Globe and Mail/Reuters](https://theglobeandmail.com/investing/article-hedge-funds-deliver-double-digit-returns-in-2024)), 2025년 +10.2% ([Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | 분산하고 위험 예산을 둔다 |
| Citadel Securities | 마켓메이커 | 초~분 | 2025년 순트레이딩수익 약 $12.2B, 전년 대비 +25% (Reuters, 익명 소식통) ([MarketScreener/Reuters 2026-04-24](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d)) | 한국 과징금 사례 (아래 6절) |
| Millennium | multi-manager pod | 다양 | 2024년 +15%, 2025년 +10.5% ([Globe and Mail](https://theglobeandmail.com/investing/article-hedge-funds-deliver-double-digit-returns-in-2024), [Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | 기계적인 drawdown 한도 |
| Point72 / Cubist | multi-manager / 시스템 부문 | 다양 | 미확인 | — |
| Jane Street | 프랍 마켓메이커 (ETF, 옵션) | 초~일 | 2025년 순트레이딩수익 $39.6B ([Reuters 2026-04-24](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d)). 2026년 1분기 $16.1B ([Reuters](https://ca.marketscreener.com/news/jane-street-rakes-in-record-first-quarter-trading-haul-of-16-1-billion-sources-say-ce7f5bdbdf8af521)) | 규제 위험 (인도 SEBI) |
| Hudson River Trading | 프랍 HFT | µs~일 | 2025년 약 $12.3B (Reuters, 익명 소식통) ([출처](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d)) | — |
| XTX Markets | 시스템 마켓메이커 | 초~분 | 2025년 매출 약 £3.9B (+44%), 세후이익 약 £1.7B (+33%) ([Bloomberg 2026-04-02](https://www.bloomberg.com/news/articles/2026-04-02/gerko-s-xtx-markets-earnings-rise-33-on-global-trading-surge), [Finance Magnates](https://www.financemagnates.com/forex/brokers/xtx-markets-revenue-rises-43-to-393-billion-in-2025/)) | — |
| Optiver | 옵션 마켓메이커 | µs~ | 2025년 net trading income €4.556B (+30%), 순이익 €1.769B (회사 발표) ([Optiver](https://www.optiver.com/insights/news/optiver-reports-robust-financial-results-for-2025/)) | — |
| Virtu | 상장 마켓메이커 | µs~초 | 2025년 trading income (net) $2,436.7M, 순이익 $912.3M ([SEC 10-K](https://www.sec.gov/Archives/edgar/data/1592386/000159238626000009/virt-20251231.htm)) | 감사받은 공시가 있는 몇 안 되는 HFT (Flow Traders도 상장사). 손익 구조를 볼 수 있다 |
| Jump Trading | 프랍 HFT, 크립토 | µs~ | 수익 미확인. 자회사 Tai Mo Shan이 TerraUSD 건으로 SEC와 약 $123M 합의 (2024-12) ([Finance Magnates](https://financemagnates.com/cryptocurrency/regulation/25y-after-terra-and-luna-collapse-sec-reaches-123-million-settlement-with-crypto-firm)) | 본업 밖의 위험 |
| Tower Research | 프랍 HFT | µs | 2019년 E-mini spoofing으로 $67.4M 합의 ([CFTC](https://cftc.gov/PressRoom/PressReleases/8074-19), [DOJ](https://www.justice.gov/opa/pr/tower-research-capital-llc-agrees-pay-67-million-connection-commodities-fraud-scheme)) | 주문을 냈다 취소하는 것도 규제 대상이다 |
| IMC, SIG, DRW, Flow Traders | 마켓메이커 / 프랍 | µs~일 | 2025년 수치 미확인 | — |
| AQR | 팩터 / 시스템 | 월~년 | 2025년 Apex +19.6%, Delphi +16.8%, Adaptive +24.4% ([Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | 공개 논문과 데이터가 많다 |
| Man Group / Man AHL | CTA / 시스템 | 일~월 | AHL 2025년 미확인. AlphaGPT 발표 (2025-11) ([Man](https://www.man.com/insights/what-ai-can-do-for-alpha)) | LLM 연구 자동화 + 사람의 심사 |
| Winton | CTA | 일~월 | 2025년 +7.4% ([Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | — |
| Bridgewater | 시스템 매크로 | 월~년 | Pure Alpha II 2025년 +34% (역대 최고), All Weather +20%, AIA Labs +11% ([Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)) | — |
| WorldQuant | 퀀트 (크라우드 알파) | 0.6–6.4일 (101 alphas) | 펀드 수익 미확인 | 공식 알파를 대량으로 만들어 결합한다 |
| Qube (QRT), CFM | 퀀트 multi-strategy / 시스템 | 다양 | 미확인 | CFM: 시장 미시구조 논문 |

- 2025년 hedge fund 수치는 "초기 추정치"이며 여러 회사가 논평을 거부했다 ([Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)).
- Jane Street, Citadel Securities, HRT의 수익은 "기밀"이며 Reuters가 익명 소식통을 통해 보도했다 ([Reuters](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d)).

### Inferences
- 2024–2025년에 가장 크게 번 곳은 HFT/마켓메이커다. Jane Street 한 곳의 2025년 순트레이딩수익($39.6B)이 Citadel Securities와 HRT를 합친 것보다 크다. 다만 이 수익은 속도, 자본, 거래소 연결, 위험 관리 조직에서 나온다.
- 펀드의 연수익(10–35%)과 마켓메이커의 매출은 같은 잣대로 비교할 수 없다. 앞은 투자자 수익률이고, 뒤는 비용을 빼기 전 트레이딩 매출이다.

### Gaps
- Point72/Cubist, Qube, CFM, Man AHL, IMC, SIG, DRW, Flow Traders의 2024–2025년 수익률/매출은 신뢰할 출처를 찾지 못했다.
- Medallion의 2024–2025년 연간 수익률은 찾지 못했다 (2차 보도로 떠도는 수치도 확인되지 않았다).

## 2. Renaissance Technologies / Medallion

### Takeaway
공개된 핵심은 "통계적으로 유의한 약한 패턴 수천 개, 짧은 보유, 하나로 통합된 모델, 남보다 깨끗한 데이터, 위험을 이해한 뒤의 레버리지"다. 구체적인 모델(HMM 등)은 공개되지 않았다. 흔히 말하는 HMM/Baum–Welch 연결은 인물들의 배경에서 나온 추정이다.

### Cited Findings
- **보유 기간과 시간 분할:** Berlekamp가 단기 매매로 옮겼다. 보유는 약 반나절~1.5주였고, 거래일을 반, 4분의 1, 결국 5분 단위로 쪼개 패턴을 찾았다 (Zuckerman 책 요약) — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **50.75%:** Mercer의 말 "We're right 50.75 percent of the time…" — 작은 우위를 아주 많이 반복한다는 뜻 — [Novel Investor (책 인용)](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **약한 신호를 많이:** p-value가 0.01 미만이면 신호를 추가했다. 1997년쯤에는 신호의 절반 이상이 "왜 되는지 완전히는 설명 못 하는" 것이었고, 터무니없지 않으면 썼다. 2010년경에는 여러 신호를 결합한 주식 거래가 대부분이었다 — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **단일 모델:** 시장마다 따로 만들지 않고 하나로 통합한 시스템을 썼다. 새 자산과 신호를 추가하기 쉬웠고, 직원이 소스 코드를 읽고 고칠 수 있었다 — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **데이터 정제:** Sandor Straus가 여러 출처의 가격을 합치고 정제해 자체 과거 데이터베이스를 만들었다. 경쟁사보다 정확했고 큰 이점이었다 — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **레버리지와 위험:** 위험, 비용, 시장충격, 시장 구조를 이해한 뒤 레버리지를 크게 썼다. 전략이 안 통하거나 변동성이 오르면 시스템이 포지션을 줄였다. 하루 15만~30만 건으로 나눠 거래해 가격을 덜 움직였다 — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **인물:** Leonard Baum(Baum–Welch 공동 저자)이 Simons의 첫 영입이었다. Peter Brown과 Robert Mercer는 1993년 IBM Research(음성인식/전산언어학)에서 왔다. *The Quants*는 IBM 출신 음성인식 전문가들을 뽑았다고 쓴다 — [Wikipedia](https://en.wikipedia.org/wiki/Renaissance_Technologies). Brown·Mercer의 IBM HMM 논문 목록 — [dblp](https://dblp.uni-trier.de/pid/04/359.html)
- **수익률:** 1988–2018 세전 평균 66.1%(복리 63.3%) — Cornell 논문, 수치는 Zuckerman 책 인용 ([II](https://www.institutionalinvestor.com/article/2bswymr8cih3jeaslxc00/portfolio/famed-medallion-fund-stretches-explanation-to-the-limit-professor-claims)). 1988–2021 세전 66%, 세후 39%, 2020년 +76% ([II 2024-11-14](https://www.institutionalinvestor.com/article/2e0uykr3vn5booz0smrcw/hedge-funds/renaissances-2024-rebirth)). 2008년 +98.2% ([Wikipedia, Bloomberg 2015 인용](https://en.wikipedia.org/wiki/Renaissance_Technologies))
- **반론:** 한 arXiv 논문은 Medallion의 실제 세전 연복리 수익이 35% 미만일 가능성이 높다고 보고, 보도된 수치를 믿기 어렵다고 한다 — [arXiv 2405.10917](https://arxiv.org/pdf/2405.10917v2)
- **외부 펀드와의 차이:** 2020년 RIEF −19.4%, RIDA −31.6%로 Medallion(+76%)과 정반대였다. RIEF 2024년 +22.7% (2011년 이후 최고) — [II 2024-11](https://www.institutionalinvestor.com/article/2e0uykr3vn5booz0smrcw/hedge-funds/renaissances-2024-rebirth), [II](https://www.institutionalinvestor.com/article/2bswzoz7x91fqht87wvsw/portfolio/the-famed-medallion-fund-is-crushing-it-other-rentech-funds-not-so-much)
- **기타:** 1993년부터 외부 투자자 신규 가입을 받지 않았고 2005년에 마지막 외부 투자자 지분을 사들였다. 2021-09 옵션 구조 세금 분쟁으로 최대 $7B를 내기로 합의 — [Wikipedia](https://en.wikipedia.org/wiki/Renaissance_Technologies). (Institutional Investor는 외부 폐쇄를 2005년으로 쓴다. 날짜 서술이 조금씩 다르다.)
- **2007 quant crash에서 두 펀드 모두 큰 손실을 봤다** — [Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)
- **2017년 (오래된 사실):** 선거 뒤 Medallion 규모 한도(약 $10B)를 올렸다. 목표는 오래전부터 "수수료 뒤 연 40%"였다 — [Bloomberg via WealthManagement, 2017-08-16](https://www.wealthmanagement.com/alternative-investments/renaissance-s-medallion-made-stunning-shift-after-trump-election)

### Inferences
- 책 요약에는 Renaissance가 HMM을 "명시적으로" 썼다는 서술이 없다. HMM/Baum–Welch 이야기는 Baum과 IBM 음성인식(HMM) 출신 Brown·Mercer의 배경에서 나온 추정이다. "Renaissance는 HMM으로 돈을 번다"는 확인된 사실이 아니다.
- 같은 회사의 외부 펀드(RIEF, 보유 기간이 긺)는 Medallion만큼 못 했다. 높은 수익은 짧은 호흡과 규모 한도(용량 제약)에서 나온다는 해석이 자연스럽다.

### Gaps
- 신호 개수, 레버리지 배수, 실제 모델 종류는 공개된 적이 없다.
- Medallion의 2024–2025년 연간 수익률은 신뢰할 출처를 찾지 못했다.

## 3. Multi-strategy 헤지펀드: D. E. Shaw, Two Sigma, Citadel, Millennium, Point72/Cubist

### Takeaway
이 회사들의 공개된 비법은 특정 모델이 아니라 조직 구조다. 서로 독립적인 PM/전략 수백 개에 자본을 나눠 주고, 손실 한도를 기계적으로 집행한다. 개인이 가져갈 것은 "알고리즘마다 위험 예산과 퇴출 규칙을 둔다"는 원칙이다.

### Cited Findings
- **Millennium 손실 한도:** pod가 고점 대비 약 5% 잃으면 자본을 줄이고, 7.5%를 넘으면 사실상 퇴출된다 (WSJ 보도를 eFinancialCareers가 인용). 예외도 있다고 한다 — [eFinancialCareers](https://www.efinancialcareers.com/news/millennium-hedge-fund-jobs), [AOL](https://www.aol.com/strategy-69-billion-hedge-fund-193733084.html). 정확한 측정 방식은 확인되지 않았다.
- **2025년:** Millennium +10.5%가 2020년 이후 처음으로 Citadel Wellington(+10.2%)을 앞섰다. 둘 다 더 작은 multi-manager들보다 뒤졌다 (FT) — [Hedgeweek](https://www.hedgeweek.com/millennium-and-citadel-post-double-digit-2025-gains-but-lag-smaller-hedge-funds/), [Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)
- **2024년:** Citadel Wellington +15.1%, Millennium +15%, D. E. Shaw Composite +18%, Oculus +36.1% (역대 최고) — [Globe and Mail/Reuters](https://theglobeandmail.com/investing/article-hedge-funds-deliver-double-digit-returns-in-2024), [II](https://www.institutionalinvestor.com/article/2eaxu6g8f1zzvc4ipdc74/hedge-funds/d-e-shaw-tops-a-2024-hedge-fund-ranking)
- **Two Sigma 모델 조작 사건:** SEC는 2025-09-11 전 직원 Jian Wu를 기소했다. 2021–2023년에 모델 14개 이상을 배포 뒤 몰래 바꿔 고객에게 최소 $165M 피해를 주고 자기 보상을 약 $23M 늘렸다는 혐의다 (미확정, 본인은 도주 중) — [SEC LR-26398](https://www.sec.gov/enforcement-litigation/litigation-releases/lr-26398), [Caixin](https://www.caixinglobal.com/2025-09-16/chinese-ex-employee-of-us-hedge-fund-two-sigma-faces-fraud-charges-102363069.html)
- **Two Sigma 회사 합의:** 알고리즘 모델 취약점 관리 실패로 SEC에 $90M를 내기로 합의했다 — [GlobalTrading](https://www.globaltrading.net/?p=37298)
- **Two Sigma 2024년:** Absolute Return Enhanced +14.3%, Spectrum +10.9% (Reuters 분석 인용 기사. 문장이 뒤섞여 있어 짝지음은 불확실) — [Yahoo Finance](https://finance.yahoo.com/news/alphabet-inc-googl-among-billionaire-232031627.html)

### Inferences
- Millennium의 5%/7.5% 규칙은 "한 전략이 망가져도 전체가 망가지지 않게" 하는 장치다. krx-quant의 알고리즘 ensemble에서는 "적중률이 일정 기간 기준 아래로 떨어지면 가중치를 줄이고, 더 떨어지면 뺀다"로 옮길 수 있다.
- Two Sigma 사건이 주는 교훈은 모델의 파라미터나 코드를 바꾸면 반드시 이력과 리뷰가 남아야 한다는 것이다. 혼자 하는 프로젝트라도 "누가, 언제, 왜 바꿨는지"가 없으면 성과 하락의 원인을 찾을 수 없다.

### Gaps
- D. E. Shaw와 Two Sigma의 구체적인 신호/모델은 공개되지 않았다. Two Sigma Insights 등 회사 연구 페이지는 이번 조사에서 확인하지 못했다.
- Point72/Cubist의 기법과 수익은 신뢰할 출처를 찾지 못했다.

## 4. HFT / 마켓메이커

### Takeaway
2025년 이 부류의 매출은 사상 최대였다 (Jane Street $39.6B, Optiver NTI €4.6B, XTX 매출 £3.9B, Virtu trading income $2.4B). 하지만 수익원은 호가 스프레드, 리베이트, 속도, 재고 위험 관리다. 신호만 내고 직접 주문하지 않는 개인은 이 수익원에 접근할 수 없다. 대신 이들이 낸 규제 사건이 "하지 말아야 할 것" 목록이 된다.

### Cited Findings
- **Jane Street:** 2025년 순트레이딩수익 $39.6B (기록), 2026년 1분기 $16.1B (기록) (Reuters, 익명 소식통) — [Reuters 2026-04-24](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d), [Reuters Q1](https://ca.marketscreener.com/news/jane-street-rakes-in-record-first-quarter-trading-haul-of-16-1-billion-sources-say-ce7f5bdbdf8af521)
- **Jane Street 인도:** SEBI는 2025-07-03 임시 명령에서 Bank Nifty 지수를 두 다리 구조로 조작해 ₹4,844 crore를 부당하게 벌었다고 주장했다. 2025-07-18 해당 금액을 escrow에 넣고 거래 제한이 풀렸다. Jane Street는 SAT에 항소했고, 2026-10-06 기준 결론이 나지 않았다 — [Moneylife 설명](https://moneylife.in/article/explainer-what-are-sebis-findings-on-jane-street-groups-market-manipulation/77594.html), [Outlook Business](https://www.outlookbusiness.com/amp/story/markets/jane-street-vs-sebi-sat-admits-appeal-against-market-regulator-directs-for-clarification-in-three-weeks), [Business Today 2026-10-06](https://www.businesstoday.in/markets/story/sebi-tells-sat-that-jane-street-has-enough-data-to-respond-to-market-manipulation-charges-559953-2026-10-06)
- **Jane Street 2026년 7월 (2차 보도, 미확인):** 2016년 이후 첫 손실 월이라고 보도됐다. AI 주식 급락과 아시아 주식 헤지 실패가 원인으로 꼽혔다. 같은 기사 안에서 손실 규모 서술이 서로 맞지 않는다 — [IBTimes 2026-08-18](https://www.ibtimes.co.uk/jane-street-surpasses-2025-revenue-despite-july-loss-1814942)
- **Citadel Securities:** 2025년 약 $12.2B (+25%). **HRT:** 약 $12.3B (둘 다 익명 소식통) — [Reuters](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d)
- **Optiver:** 2025년 NTI €4.556B (2024년 €3.494B), 순이익 €1.769B — [Optiver 발표](https://www.optiver.com/insights/news/optiver-reports-robust-financial-results-for-2025/)
- **XTX:** 2025년 세후이익 약 £1.71B (2024년 £1.28B). Gerko 지분 약 75% — [Bloomberg 2026-04-02](https://www.bloomberg.com/news/articles/2026-04-02/gerko-s-xtx-markets-earnings-rise-33-on-global-trading-surge), [Finance Magnates](https://www.financemagnates.com/forex/xtx-markets-posts-50-profit-jump-to-128-billion-on-trading-surge)
- **Virtu (상장, 감사받은 수치):** 2025년 trading income (net) $2,436.7M (+33.7%), Adjusted NTI $2,145.3M, 순이익 $912.3M — [SEC 10-K FY2025](https://www.sec.gov/Archives/edgar/data/1592386/000159238626000009/virt-20251231.htm)
- **Tower Research (오래된 사실, 2019):** 2012–2013년 트레이더 3명의 CME E-mini spoofing으로 DOJ DPA와 CFTC에 합계 $67.4M — [CFTC](https://cftc.gov/PressRoom/PressReleases/8074-19), [DOJ](https://www.justice.gov/opa/pr/tower-research-capital-llc-agrees-pay-67-million-connection-commodities-fraud-scheme)
- **Jump (Tai Mo Shan):** TerraUSD 페그를 떠받친 매수로 시장을 오도했다는 혐의로 SEC와 약 $123M 합의 (2024-12, 인정도 부인도 하지 않음) — [Finance Magnates](https://financemagnates.com/cryptocurrency/regulation/25y-after-terra-and-luna-collapse-sec-reaches-123-million-settlement-with-crypto-firm)

### Inferences
- 공개 자료로는 HFT 회사의 "신호"를 거의 알 수 없다. 공개되는 것은 매출 규모와 규제 사건이다. 규제 사건 셋(Tower, Jane Street, 아래 Citadel Securities 한국 건)은 모두 "주문의 모양"이 문제였다. 예측력이 문제였던 경우는 없다.
- 4분 뒤를 예측하는 개인 엔진은 이들과 같은 시장에서 같은 가격을 본다. 1분 이하의 미시 패턴은 이들이 먼저 가져간다고 보는 것이 안전하다.

### Gaps
- IMC, SIG, DRW, Flow Traders의 2025년 실적은 이번 조사에서 확인하지 못했다.
- HRT, XTX의 기법(ML 사용 정도 등)은 1차 출처로 확인하지 못했다.

## 5. 시스템 / 팩터 운용사: AQR, Man AHL, Winton, Bridgewater, WorldQuant, Qube, CFM

### Takeaway
연구를 가장 많이 공개하는 쪽이다. 개인이 실제로 읽고 따라 할 수 있는 자료는 대부분 여기서 나온다. AQR의 momentum/value 논문과 데이터, WorldQuant의 공식 알파 101개, CFM/Bouchaud의 시장충격 연구가 대표적이다.

### Cited Findings
- **WorldQuant "101 Formulaic Alphas" (Kakushadze, arXiv 2016-01, Wilmott 2016):** 실제 거래에 쓰인 알파 101개를 공식과 코드로 공개했다. 평균 보유 0.6–6.4일, 알파끼리 평균 상관 15.9%로 낮다. 수익은 변동성과 강하게 상관되고 회전율과는 유의한 관계가 없다 — [arXiv 1601.00991](https://arxiv.org/abs/1601.00991)
- **WorldQuant BRAIN:** 외부 사용자가 알파를 시뮬레이션하고 제출하는 웹 플랫폼. 2022년 시작. 회사 사이트 기준 사용자 250,000명 이상, consultant 9,000명 이상 (회사 자료, 독립 검증 아님) — [WorldQuant](https://www.worldquant.com/brain), [출시 발표](https://www.worldquant.com/ideas/worldquant-launches-brain-platform-and-inaugural-global-alphathon-competition)
- **AQR 논문:** "Time Series Momentum" (Moskowitz, Ooi, Pedersen). 선물/선도 58개에서 자기 과거 12개월 수익이 다음 수익을 예측하고, 약 1년 지속된 뒤 일부 되돌린다 — [AQR](https://www.aqr.com/library/journal-articles/time-series-momentum), [SSRN](https://papers.ssrn.com/abstract=2089463). "Value and Momentum Everywhere" (Asness, Moskowitz, Pedersen). 8개 시장에서 value와 momentum이 서로 음의 상관이고, 데이터를 공개했다 (2024-02 갱신) — [AQR 데이터](https://www.aqr.com/Insights/Datasets/Value-and-Momentum-Everywhere-Portfolios-Monthly)
- **CFM / Bouchaud:** metaorder 크기 Q에 대해 가격충격이 √Q에 비례한다는 square-root law — [Bouchaud Substack](https://bouchaud.substack.com/p/the-square-root-law-of-market-impact), [Tóth et al. 2011, arXiv 1105.1694](https://www.arxiv.org/pdf/1105.1694). 2025년 도쿄증권거래소 데이터 연구는 이 법칙이 개별 child order 수준에서도 성립하고 충격이 "기계적"이라고 결론냈다 — [RePEc 2502.16246](https://ideas.repec.org/p/arx/papers/2502.16246.html)
- **Man Group AlphaGPT (2025-11-13):** "Idea Person"(가설), "Implementer"(Python 코드), "Evaluator"(통계, 위험, 경제적 근거 검증) 세 agent를 orchestrator가 묶는다. 사람 신호와 같은 통과 기준을 적용한다. multiple testing(p-hacking) 위험을 스스로 인정하고 로그, 투자위원회 심사, 코드 리뷰를 둔다. 지금까지는 systematic equity에서 가장 잘 됐다 — [Man Institute](https://www.man.com/insights/what-ai-can-do-for-alpha). Bloomberg를 인용한 보도는 수십 개 신호가 실거래 승인을 받았다고 한다 — [Hedgeweek](https://hedgeweek.com/news/man-group-deploys-agentic-ai-for-quant-signal-discovery)
- **2025년 수익률:** Bridgewater Pure Alpha II +34%, All Weather +20%, AIA Labs +11%. AQR Apex +19.6%, Delphi +16.8%, Adaptive +24.4%. Winton +7.4% — [Fortune 2026-01-02](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)

### Inferences
- 101 alphas의 보유 기간(0.6–6.4일)은 krx-quant의 4분 호흡보다 훨씬 길다. 공식을 그대로 가져올 수는 없다. 다만 "가격, 거래량, 순위(rank), 시차 상관 같은 단순 연산을 조합해 서로 상관이 낮은 약한 신호를 많이 만든다"는 방식은 분봉에도 적용할 수 있다.
- square-root law는 개인의 작은 주문에는 충격이 거의 없다는 뜻이기도 하다. 하지만 4분 호흡에서는 충격보다 호가 스프레드와 거래 비용이 더 큰 장벽이다.
- Man의 AlphaGPT가 가장 강조하는 위험은 multiple testing이다. 알고리즘을 계속 추가하는 krx-quant에도 똑같이 적용된다.

### Gaps
- Qube(QRT)와 CFM의 수익률, Man AHL의 2025년 수익률은 찾지 못했다.
- Bridgewater AIA Labs가 AI를 구체적으로 어떻게 쓰는지는 1차 출처로 확인하지 못했다.

## 6. 한국 시장: 외국계 HFT, 규제 사례, 국내 퀀트

### Takeaway
한국에서 알고리즘/HFT의 비중은 이미 크다. 2026년 2월 등록 HFT 계좌 2,266개가 거래대금의 약 58%를 차지했다 (금감원 자료, 보도). 규제는 2023년 등록제와 Citadel Securities 과징금 이후 강해지는 중이다. 국내 사례(한맥투자증권)는 알고리즘 입력 오류 한 번이 회사를 파산시킬 수 있음을 보여 준다.

### Cited Findings
- **Citadel Securities 과징금 (2023-01-26 증선위):** 해외 소재 증권사(언론 보도상 Citadel Securities)가 2017-10~2018-05 메릴린치 서울지점을 통해 264개 종목에서 고빈도 알고리즘 매매로 시장질서를 교란했다는 이유로 ₩118.8억을 부과받았다. 초단타 매매에 대한 첫 과징금이자 시장교란 건 역대 최대였다. 수법: IOC 주문으로 최우선 매도호가를 반복해 소진하고, 생긴 호가 공백에 지정가 매수로 새 최우선 매수호가를 만든 뒤 취소하는 것을 짧게 집중 반복했다 — [금융위 보도참고](https://www.fsc.go.kr/no010101/79339), [뉴시스 2023-01-27](https://mobile.newsis.com/view/NISX20230127_0002172348)
- **행정소송:** Citadel이 불복해 소송을 냈고, 2024-12 기준 1심이 진행 중이었다 — [뉴시스 2024-12-29](https://mobile.newsis.com/view/NISX20241227_0003012977). 판결 결과는 찾지 못했다.
- **고빈도 알고리즘 거래자 등록제:** 2023-01-25 시행, 3개월 유예 뒤 2023-04-25부터 의무. 요건은 전산설비의 안정성과 적정성, 거래 전문성, 내부통제다. 거래소가 거래자별 식별코드를 붙여 감시하고, 증권사는 주문 시스템을 미리 점검한다 — [금융위 보도참고](https://www.fsc.go.kr/no010101/79339). 대상은 주문 건수가 아니라 "고속" 주문 인프라 사용자로 정했다 — [머니투데이 2023-01-27](https://www.mt.co.kr/stock/2023/01/27/2023012715350053929)
- **2024-12 당국:** 기존 시장교란 유형에 알고리즘/초단타 유형이 없어 Citadel 건은 "그 외의 사항"으로 처리해야 했다. 반복적인 소량 주문으로 거래가 활발한 것처럼 보이게 하는 것을 시장질서 교란으로 규제한다고 밝혔다 — [뉴시스 2024-12-29](https://mobile.newsis.com/view/NISX20241227_0003012977)
- **HFT 비중 (2026-04 보도):** 금감원이 김승원 의원실에 낸 자료에 따르면 2026년 2월 등록 HFT 계좌 2,266개의 거래대금은 약 ₩3,927조로, 같은 달 전체 약 ₩6,797조의 약 58%였다. HFT의 상당 부분이 파생상품에 몰려 있다. 과다호가부담금 도입 법안이 발의됐다 — [국민일보 via Daum, 2026-04-06](https://v.daum.net/v/20260406104301578)
- **한맥투자증권 (오래된 사실, 2013):** 2013-12-12 KOSPI200 옵션 주문에서 이자율 변수를 "잔존일수/365" 대신 "잔존일수/0"으로 입력해 비정상 호가가 나갔고, 약 ₩462억 손실을 봤다. 상대방 미국계 헤지펀드는 약 ₩360억 이익을 돌려주지 않았고, 대법원은 2023-05 거래 취소를 인정하지 않았다. 회사는 2015-02 파산 선고를 받았다 — [이투데이](https://www.etoday.co.kr/news/view/2248804), [이투데이 (금감원 결론)](https://www.etoday.co.kr/news/view/849470)
- **국내 퀀트 (회사 자료 위주):** 크래프트테크놀로지스(Qraft)는 2016년 설립된 AI 자산운용 스타트업으로, NYSE에 AI ETF(QRFT, AMOM 등)를 상장했고 2022-01 SoftBank에서 약 ₩1,700억을 투자받았다고 보도됐다 — [이코노미스트 2022-01](https://economist.co.kr/article/view/ecn202201110035), [아이뉴스24](https://www.inews24.com/view/1404578)
- **학술:** 한국 ELW/ETF/주식시장 HFT 연구는 HFT가 호가를 더 많이 내고, 정정보다 취소가 많고, 추세추종 성향이 있다고 보고했다 (박수철, 2020; 검색 요약 기준, 원문 미확인) — [PDF](https://www.smallake.kr/wp-content/uploads/2023/02/한국_증권시장의_고빈도거래_현황_및_특성.pdf)

### Inferences
- 58%라는 비중의 분모(₩6,797조/월)는 현물 주식만으로는 너무 크다. 파생상품 명목 거래대금이 포함된 것으로 보인다. 삼성전자, SK하이닉스 같은 대형주 현물에서의 비중은 이 숫자와 다를 수 있다.
- Citadel 건의 수법(최우선호가 소진 → 공백에 호가 생성 → 취소)은 4분 예측 엔진이 "호가 불균형"을 특징으로 쓸 때 그 신호가 HFT의 의도적인 호가 조작으로 오염될 수 있음을 보여 준다.
- krx-quant는 주문을 내지 않으므로 등록제 대상이 아니다. 나중에 자동 주문을 붙이면 "고속 주문 인프라"에 해당하는지, 반복 주문/취소 패턴이 교란 유형에 걸리는지 확인해야 한다.

### Gaps
- KRX가 공식 발표한 알고리즘/HFT 비중 통계(시장별, 투자자별)는 찾지 못했다.
- Citadel Securities 행정소송 1심 판결 결과는 찾지 못했다.
- KOSPI200 선물/옵션에서 활동하는 외국계 마켓메이커 이름과 점유율은 신뢰할 출처를 찾지 못했다. 국내 증권사 프랍 데스크의 알고리즘 운용 실태도 확인하지 못했다. ELW 스캘퍼 사건(2011)은 이번 조사에서 다루지 못했다.

## 7. 공통 테마와 개인에게 현실적인 것

### Takeaway
공통 테마는 다섯 가지다. 신호의 폭(breadth), 위험 한도, 비용과 체결, 인프라와 데이터, 연구 규율. 개인이 가장 싸게 따라 할 수 있는 것은 위험 한도, 데이터 정제, 연구 규율이다. 가장 비싸고 불가능한 것은 인프라와 속도다.

### Cited Findings
- **Breadth:** Renaissance는 p<0.01 신호를 계속 추가했다 ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)). WorldQuant 알파 101개의 평균 상관은 15.9%였다 ([arXiv](https://arxiv.org/abs/1601.00991)).
- **위험 한도:** Millennium pod 5%/7.5% ([eFinancialCareers](https://www.efinancialcareers.com/news/millennium-hedge-fund-jobs)). Renaissance는 변동성이 오르면 포지션을 줄였다 ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)).
- **비용/체결:** square-root impact ([Bouchaud](https://bouchaud.substack.com/p/the-square-root-law-of-market-impact)). Renaissance는 거래를 하루 15만~30만 건으로 쪼갰다 ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)).
- **데이터:** Straus의 정제된 가격 DB ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)). 한국 HFT 등록 요건 1번이 전산설비의 안정성이다 ([금융위](https://www.fsc.go.kr/no010101/79339)).
- **연구 규율과 거버넌스:** Man은 multiple testing을 경계하고 사람 심사를 둔다 ([Man](https://www.man.com/insights/what-ai-can-do-for-alpha)). Two Sigma 사건은 모델 변경 통제의 실패였다 ([SEC](https://www.sec.gov/enforcement-litigation/litigation-releases/lr-26398)).
- **운영 위험:** 한맥의 입력값 하나 ([이투데이](https://www.etoday.co.kr/news/view/2248804)). 2007 quant crash에서 Renaissance도 큰 손실을 봤다 ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)).

### Inferences
- 교과서적 정리(Grinold의 fundamental law: IR ≈ IC × √breadth)로 보면, 종목 3개만 다루는 krx-quant의 breadth는 종목 수가 아니라 "하루 동안 내리는 독립적인 판단의 수"와 "서로 상관이 낮은 신호의 수"에서 나와야 한다. 세 종목은 모두 반도체 관련이라 서로 상관이 높다. 세 종목 신호가 동시에 맞거나 틀리는 날이 많을 것이다.
- Renaissance의 "50.75%"는 거래 수천~수만 건과 레버리지가 있어야 의미가 있다. 상승/보합/하락 3분류 적중률과는 기준선이 다르므로(무작위 기준선이 50%가 아님) 직접 비교하면 안 된다.

### Gaps
- 개인 규모에서 이 원칙들이 실제로 성과를 냈다는 체계적인 연구는 찾지 못했다.

## 8. 최근 뉴스 (2023–2026): 수익, 전략 변화, AI/LLM

### Takeaway
2024–2025년은 multi-strategy와 마켓메이커 모두 좋은 해였다. 2025년 hedge fund 평균은 2009년 이후 최고 수준으로 보도됐다. AI/LLM은 "연구 자동화(가설→코드→백테스트)" 형태로 들어오고 있고, 대형사도 사람의 심사를 빼지 않는다.

### Cited Findings
- 2025년 hedge fund 평균 약 12.6%로 2009년 이후 최고 (보도 요약), 대부분 S&P 500에 뒤졌다 — [Yahoo Finance](https://finance.yahoo.com/news/2025-returns-hedge-funds-rolling-234728986.html), [Hedgeweek](https://www.hedgeweek.com/millennium-and-citadel-post-double-digit-2025-gains-but-lag-smaller-hedge-funds/)
- Bridgewater Pure Alpha II 2025년 +34%로 역대 최고, AI 기반 펀드 AIA Labs +11% — [Fortune](https://fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium/)
- Man Group AlphaGPT (2025-11): LLM agent가 신호를 만들고, 사람과 같은 기준으로 심사한다 — [Man](https://www.man.com/insights/what-ai-can-do-for-alpha), [Hedgeweek](https://hedgeweek.com/news/man-group-deploys-agentic-ai-for-quant-signal-discovery)
- Jane Street 2025년 $39.6B, 2026년 1분기 $16.1B. 2026년 7월 첫 손실 월이라는 보도가 있다 (미확인) — [Reuters](https://www.marketscreener.com/news/jane-street-s-40-billion-trading-haul-tops-rivals-sources-say-ce7f59dfdf8ff52d), [IBTimes](https://www.ibtimes.co.uk/jane-street-surpasses-2025-revenue-despite-july-loss-1814942)
- Two Sigma: 모델 조작 기소 (2025-09)와 SEC $90M 합의 — [SEC](https://www.sec.gov/enforcement-litigation/litigation-releases/lr-26398), [GlobalTrading](https://www.globaltrading.net/?p=37298)
- 한국: HFT 비중 약 58% 보도와 과다호가부담금 법안 (2026-04) — [국민일보 via Daum](https://v.daum.net/v/20260406104301578)

### Inferences
- LLM이 연구를 빠르게 하면 시도 횟수가 늘고, 그만큼 우연히 좋아 보이는 신호도 늘어난다. Man이 같은 통과 기준과 사람 심사를 고집하는 이유다.

### Gaps
- Renaissance, Two Sigma, D. E. Shaw, HRT의 AI/LLM 사용에 대한 1차 출처는 찾지 못했다.
- Medallion과 Two Sigma 주력 펀드의 2025년 연간 수익률은 확인하지 못했다.

## 9. krx-quant 적용 메모

### Takeaway
따라 할 것은 다섯 가지다. 상관 낮은 약한 신호를 많이 결합하기, 엄격한 통계 기준과 multiple testing 보정, 데이터 정제, 알고리즘별 위험 한도와 퇴출 규칙, 모델 변경 이력 관리. 따라 할 수 없는 것도 있다. 속도 경쟁, 레버리지, 마켓메이킹 수익원, 수천 종목의 breadth, 대형 연구 조직이다.

### Cited Findings
- 근거는 위 2–7절의 출처와 같다. 핵심은 Renaissance의 p<0.01·단일 모델·Straus 데이터 ([Novel Investor](https://novelinvestor.com/notes/the-man-who-solved-the-market-by-gregory-zuckerman/?nb=1)), 101 alphas의 낮은 상관 ([arXiv](https://arxiv.org/abs/1601.00991)), Millennium 5%/7.5% ([eFinancialCareers](https://www.efinancialcareers.com/news/millennium-hedge-fund-jobs)), Man의 multiple testing 경계 ([Man](https://www.man.com/insights/what-ai-can-do-for-alpha)), Citadel 한국 건 ([금융위](https://www.fsc.go.kr/no010101/79339)), 한맥 사고 ([이투데이](https://www.etoday.co.kr/news/view/2248804))다.

### Inferences
**따라 할 것**
- **약한 신호를 많이, 서로 다르게:** ensemble 안의 알고리즘들이 내는 예측끼리 상관을 정기적으로 재고, 서로 비슷한 알고리즘은 하나로 친다. 새 알고리즘은 "적중률이 높은가"보다 "기존 것과 다른 정보를 주는가"로 판단한다.
- **통과 기준을 엄격하게:** 알고리즘이 늘수록 우연히 통과하는 것도 는다. 사전 검증(pre-validation)의 기준을 시도한 알고리즘 수에 맞춰 올리고, 검증 기간은 학습에 쓰지 않는다.
- **데이터 정제가 먼저:** 시가/종가 동시호가, VI 발동, 거래정지, 체결 시각 오류, 결측 분봉을 4분 예측의 입력과 정답 양쪽에서 따로 다룬다. Straus의 교훈은 "깨끗한 데이터가 곧 우위"라는 것이다.
- **시간을 잘게 쪼갠 패턴:** Renaissance는 거래일을 5분 단위로 쪼갰다. 4분 예측도 장 시작 직후, 점심, 장 마감 전처럼 시간대별로 성적을 따로 보고, 시간대를 조건으로 쓰는 것이 자연스럽다.
- **알고리즘별 위험 한도:** Millennium처럼 기계적으로 집행한다. 예를 들어 최근 N일 적중률이 기준선 아래로 X 떨어지면 가중치를 반으로, 더 떨어지면 0으로 한다. 사람이 그때그때 판단하지 않는다.
- **비용을 넘는 신호만:** 4분 호흡에서는 호가 스프레드, 수수료, 매도 시 세금이 예상 움직임을 쉽게 넘는다. "맞혔다"와 "비용을 빼고 벌었다"를 따로 기록한다.
- **모델 변경 이력:** Two Sigma 사건처럼 파라미터나 코드가 몰래 바뀌면 원인 분석이 불가능하다. 알고리즘마다 버전, 변경 이유, 변경 전후 성적을 남긴다.
- **입력 검증:** 한맥은 변수 하나("/0")로 파산했다. 신호만 내더라도 이상한 입력(가격 0, 급격한 호가 공백)에서 신호를 막는 가드가 필요하다.
- **호가 특징의 오염 주의:** Citadel 건처럼 HFT가 일부러 최우선호가를 만들고 지울 수 있다. 호가 불균형 같은 특징은 짧게 나타났다 사라지는 호가에 약하다. 체결 기반 특징과 함께 쓴다.

**따라 할 수 없는 것**
- **속도와 마켓메이킹:** µs 단위 경쟁과 스프레드/리베이트 수익은 HFT의 영역이다. 개인 API의 지연(초 단위)으로는 1분 이하 패턴을 이길 수 없다.
- **레버리지와 특수 구조:** Renaissance의 레버리지와 은행 옵션 구조는 개인에게 없고, 있어도 위험하다.
- **종목 breadth:** 수천 종목에 분산하는 효과를 3종목, 그것도 같은 반도체 업종에서는 얻을 수 없다.
- **조직:** 수백 명의 PM, 연구자, 대체 데이터 구매, 24시간 인프라 팀은 없다.
- **보도된 수익률을 목표로 삼는 것:** 대부분 익명 보도와 생존 편향이 섞인 수치다. Medallion 수치에는 학술적 반론도 있다.

### Gaps
- 위 적용 아이디어들이 4분 호흡의 KRX 대형주에서 실제로 효과가 있는지는 krx-quant 자체 데이터로 검증해야 한다. 이번 조사에서 같은 조건의 공개 연구는 찾지 못했다.
