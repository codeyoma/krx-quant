# 스윙 트레이딩 노트(04) 보강: 미확인 항목 해소, 정정, 한국·아시아 근거 추가

기준일: 2026-10-17. 연구 정리이며 투자 조언이 아니다. 대상 노트: `단기 매매 퀀트 기법 조사/04_swing_trading.md`.
표기:
- **(미확인)**: 이번에도 원문이나 초록으로 확인하지 못한 것.
- **(오래된 결과)**: 표본이 2010년 이전에 끝난 결과.
- **(2차 출처)**: 원문이 아니라 요약 기사나 강의 자료에서 확인한 것.

조사 방법:
- 초록은 RePEc IDEAS 논문 페이지와 OpenAlex API에서 읽었다. Elsevier 논문은 OpenAlex와 Semantic Scholar에 초록이 없어서 IDEAS를 썼다.
- 서지는 Crossref API로 확인했다.
- 원문 PDF로 수치를 확인한 논문은 넷이다: MOP 2012(Pedersen 홈페이지), HOP 2017(AQR), J-T 1993(JSTOR 사본), Zhao 2026(arXiv v4).
- WebSearch는 9회 썼고 한도 문제는 없었다.

---

## 1. 이전 노트의 (미확인)·Gaps 항목 대조표

### Takeaway
이전 노트의 (미확인) 항목과 Gaps 항목 38개를 대조했다. 결과는 다음과 같다.
- 확인 24개
- 부분 확인 8개
- 여전히 미확인 6개

정정할 내용은 다섯 가지다.
- Fung et al.(2010)은 한국 단독 연구가 아니다. 아시아 지수선물 전반을 다룬다.
- Rapach 후속 연구의 "미국 경기침체기 집중"은 초록에 없다.
- Park & Irwin은 초기 연구 기준으로 주식시장에서는 기술적 분석이 수익을 내지 못한다고 정리한다.
- Jacobs & Müller의 결론은 "해외에서 약하다"보다 강하다. 발표 후 감쇠가 확실한 나라는 미국뿐이다.
- 삼성전자와 SK하이닉스의 미국 뉴스 반응 구조가 2026-05-27 단일종목 레버리지 ETF 출시 뒤 바뀌었다는 증거가 나왔다.

### Cited Findings

| # | 이전 노트 항목 (절) | 상태 | 지금 확인된 값 / 정정 | 출처 |
|---|---|---|---|---|
| 1 | Blitz, Huij, Lansdorp & Verbeek (2013) 초록 (§1) | 확인 | Fama-French 요인 노출을 뺀 residual reversal의 위험조정 수익은 기존 reversal의 **2배**다. 비용 후에도 유의하고, 1990년 이후 대형주로 한정해도 유의하다. JFM 16(3):477–504. 정확한 Sharpe는 (미확인) | [IDEAS](https://ideas.repec.org/a/eee/finmar/v16y2013i3p477-504.html) |
| 2 | 한국 주간 반전의 비용 후 최신 논문 (§1 Gap) | 부분 | Sim (2026, EMFT): 한국 액티브 펀드가 보유를 줄일 때 과거 패자의 단기 반전이 강하다. 패시브 펀드의 이탈은 영향이 작다. 비용 후 수치는 없다 | [DOI](https://doi.org/10.1080/1540496x.2026.2617473) |
| 3 | J-T 1993 표본 기간과 월 수익 (§2) | 확인 | NYSE·AMEX, **1965–1989**. 최고 전략(12개월 형성, 3개월 보유)은 **월 1.31%**, 형성과 보유 사이를 1주 띄우면 1.49%다. 6개월 형성은 보유기간과 관계없이 약 월 1%다. (오래된 결과) | [원문 PDF](https://www.bauer.uh.edu/rsusmel/phd/jegadeesh-titman93.pdf) |
| 4 | MOP 2012 Sharpe (§2 Gap) | 확인 | 분산 TSMOM의 gross Sharpe는 **1을 넘는다**. 주식시장의 약 2.5배다. 연 변동성 12%, 1985–2009년. 58종 모두 양(+)이고 52종이 5%에서 유의하다. 1966–1985 표본 외 Sharpe는 1.1이다. 상세는 §2 | [원문 PDF](https://pages.stern.nyu.edu/~lpederse/papers/TimeSeriesMomentum.pdf) |
| 5 | HOP 2017 비용·수수료 수치 (§2) | 확인 | 1880–2016년 연 초과수익: gross 18.0%, 비용 후 11.0%, 2/20 수수료와 비용 후 **7.3%**. 변동성 9.7%, **순 Sharpe 0.76**. 2010–2016년은 순 3.3%, Sharpe 0.41 | [AQR PDF](https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf) |
| 6 | Kim, Tse & Wald (2016) 초록 (§2) | 확인 | MOP의 결과는 TSMOM보다 변동성 스케일링에서 주로 나온다. 스케일링을 빼면 TSMOM과 buy-and-hold의 누적수익과 alpha가 유의하게 다르지 않다 | [IDEAS](https://ideas.repec.org/a/eee/finmar/v30y2016icp103-124.html) |
| 7 | Barroso & Santa-Clara (2015) 초록 (§2) | 확인 | momentum 위험은 시변이며 예측할 수 있다. 위험을 관리하면 crash가 "사실상 사라지고" Sharpe가 "거의 2배"가 된다. JFE 116(1):111–120 | [IDEAS](https://ideas.repec.org/a/eee/jfinec/v116y2015i1p111-120.html) |
| 8 | Novy-Marx (2012) 초록 (§2) | 확인 + 보강 | momentum은 주로 12–7개월 전 성과에서 나온다. 최근 성과 기반 전략은 덜 수익적이고, **특히 최대형·고유동 종목에서 그렇다**. 국제 지수, 상품, 통화에서도 같다. JFE 103(3):429–453 | [IDEAS](https://ideas.repec.org/a/eee/jfinec/v103y2012i3p429-453.html) |
| 9 | Krauss, Do & Huck (2017) 수치 (§3) | 확인 | S&P 500, 1992–2015년, 하루 앞 신호. 앙상블(DNN+GBT+RF)은 k=10에서 비용 전 표본 외 **일 0.45% 초과**다. 최근 연도로 갈수록 수익이 줄어든다 | [IDEAS](https://ideas.repec.org/a/eee/ejores/v259y2017i2p689-702.html) |
| 10 | 한국 대형주 pairs의 비용 후 성과 (§3 Gap) | 미확인 | 이번에도 찾지 못했다 | — |
| 11 | copula pairs의 2015년 이후 성과 (§3 Gap) | 미확인 | 이번 범위에서 검색하지 않았다 | — |
| 12 | PEAD 2025 논쟁의 개별 논문 URL (§4) | 확인 (2차 출처) | Subrahmanyam (2025, SSRN WP 5930255): 2001.2–2024.12, t값 2.18, microcap 제외 시 1.43. Dickerson, Julliard & Mueller "The co-pricing factor zoo"(JFE 게재 예정). Hirshleifer, Peng & Wang (2025, RFS 38(3):883–937) "News diffusion in social networks and stock market reactions": drift t값 약 14 | [UCLA Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again/), [SSRN 5930255](https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=5930255), [SSRN 4589786](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4589786), [RFS](https://academic.oup.com/rfs/article-abstract/38/3/883/7698199) |
| 13 | Yun & Kim (2010) 서지가 검색 요약 기준 (§4) | 확인 + 보강 | IRFA 19(4):258–269. 이전 노트 내용에 더해, 효력일 전 선행매매(anticipatory trading)와 변동성 효과의 증거가 있다. (오래된 결과) | [IDEAS](https://ideas.repec.org/a/eee/finana/v19y2010i4p258-269.html) |
| 14 | 한국 PEAD 최근 연구 (§4 Gap) | 부분 | Goh & Jeon (2017, PBFJ): 한국에도 PEAD가 있다. 긍정 서프라이즈 종목이 52주 고가 근처일 때 강하다. Eom, Hahn & Sohn (2019, PBFJ): 개인의 뉴스 역행 매매가 PEAD를 만든다. 대형주와 잠정실적 공시에 한정한 결과는 (미확인) | [IDEAS Goh-Jeon](https://ideas.repec.org/a/eee/pacfin/v44y2017icp150-159.html), [IDEAS Eom et al.](https://ideas.repec.org/a/eee/pacfin/v53y2019icp379-398.html) |
| 15 | Chay·류혁선 (2006) URL (§4 Gap) | 부분 | 서지 확인: 증권학회지(Asia-Pacific Journal of Financial Studies) 35(1):69–101. 초록은 (미확인) | [KCI](https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART000993079) |
| 16 | 한국 지수 변경 효과의 2015년 이후 감쇠 (§4 Gap) | 부분 (비학술) | 동료심사 논문은 찾지 못했다. 언론 분석: 2017–2019년 KOSPI200 신규 편입 17종목 중 **13종목이 변경일 직후 하락**했다. 발표 후에는 12종목이 올랐다 | [뉴스토마토 2020-06-11](https://newstomato.com/ReadNews.aspx?no=977218) |
| 17 | Brock, Lakonishok & LeBaron (1992) 내용 (§5) | 확인 | DJIA 1897–1986, 이동평균과 trading-range break 규칙. 매수 신호 뒤 수익률이 높고 변동성은 낮다. 매도 신호 뒤 수익률은 음(-)이다. 네 가지 귀무모형(random walk, AR(1), GARCH-M, EGARCH)과 맞지 않는다. (오래된 결과) | [IDEAS](https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html) |
| 18 | Sullivan, Timmermann & White (1999) 내용 (§5) | 부분 | White Reality Check를 BLL의 26개 규칙에서 확장한 전체 규칙 집합에 적용했다. DJIA 100년 일별 자료다. 표본 외에서 우위가 약해진다는 결론과 수치는 초록에 없어 (미확인) | [IDEAS](https://ideas.repec.org/a/bla/jfinan/v54y1999i5p1647-1691.html) |
| 19 | Park & Irwin (2007) 수치 (§5) | 확인 + **정정** | 초기 연구에서 기술적 분석은 외환과 선물에서는 수익을 냈지만 **주식시장에서는 아니었다**. 현대 연구 95편 중 긍정 56, 부정 20, 혼합 19다. 수익성은 "적어도 1990년대 초까지"다 | [IDEAS](https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html) |
| 20 | Szakmary, Shen & Sharma (2010) 초록 (§5) | 확인 | 상품선물 28종, 월별 48년. 이중 이동평균과 채널 규칙은 모든 파라미터에서 28개 시장 중 22개 이상에서 비용 후 양(+)이다. data-mining 보정 후에도 견고하다 | [IDEAS](https://ideas.repec.org/a/eee/jbfina/v34y2010i2p409-426.html) |
| 21 | Connors RSI(2)와 한국 Turtle 검증 (§5 Gap) | 미확인 | 이번에 검색하지 않았다. 이전 결론을 유지한다 | — |
| 22 | 한국 대형주 변동성 관리 (§6 Gap) | 미확인 | 찾지 못했다 | — |
| 23 | 2011 국내 momentum 연구 서지 (§7) | 확인 | 이명철·이수건 (2011), 경영과 정보연구 30(4):147–173, 1980–2009년. 외환위기 전에는 반전, 후에는 momentum이 나타났다. 위기 뒤에는 52주 고가 전략이 가격변화 전략보다 낫고, 하락장에서도 작동한다 | [earticle](https://www.earticle.net/Article/A164768) |
| 24 | Chui, Titman & Wei의 한국 개인주의 점수 (§7) | 확인 (2차 출처) | Hofstede 개인주의 점수는 한국 **18**, 미국 91이다. Chui et al. 표본에 한국이 들어가는지는 (미확인) | [Global Cement](https://www.globalcement.com/magazine/the-last-word/1240-getting-to-know-you), [INSEAD 자료](https://publishing.insead.edu/sites/publishing/files/2024-06/6914-MTC-Individualism-Handout-CSup-EN-0-06-2024.docx) |
| 25 | Richards (2005) 세부 (§7) | 확인 | 아시아 신흥시장 6곳의 외국인 일별 총매매 자료다. 외국인은 국내 수익률뿐 아니라 **글로벌 수익률에도** positive feedback 매매를 한다. 가격 영향은 기존 추정보다 훨씬 크다 | [IDEAS](https://ideas.repec.org/a/cup/jfinqa/v40y2005i01p1-27_00.html) |
| 26 | IJMF 논문 저자 (§7) | 확인 | Yang, Samitas & Kampouris (2021), IJMF 17(4):497–521 | [DOI](https://doi.org/10.1108/IJMF-01-2020-0022) |
| 27 | Rapach 후속 연구, 검색 요약 기준 (§7) | 확인 + **정정** | Aye, Balcilar & Gupta (2017), Empirica 44(1):121–146. 1980–2014년. 미국 수익률의 예측력은 **시변이며 특정 하위기간에만 유의하다**. "미국 경기침체기에 집중된다"는 초록에 없다 → (미확인)으로 정정 | [IDEAS](https://ideas.repec.org/a/kap/empiri/v44y2017i1d10.1007_s10663-015-9313-3.html) |
| 28 | Cohen & Frazzini (2008) 수치 (§7) | 확인 | 고객사 뉴스 기반 롱숏 전략의 월 alpha는 **150bp를 넘는다** | [IDEAS](https://ideas.repec.org/a/bla/jfinan/v63y2008i4p1977-2011.html) |
| 29 | 2010 KOSPI200 선물 연구 서지 (§7) | 확인 | Kim, Choi & Lee (2010), JITAM 17(3):151–162. 미국 야간 뉴스에 한국 투자자가 과잉반응하고, KOSPI200 선물은 시가→종가에 유의하게 반전한다. 평일과 주말의 효과가 다르다. (오래된 결과) | [earticle](https://www.earticle.net/Article/A166629) |
| 30 | arXiv "Preying on Leveraged ETFs" 결론 (§7) | 확인 | Zhao (2026, 프리프린트). 상세는 §3. 출시 전에는 미국 뉴스 반응의 반전이 없었다. 출시 뒤에는 다음날 종가까지 첫날 반응의 약 75%가 되돌려진다 | [arXiv](https://arxiv.org/abs/2608.03703) |
| 31 | 프로그램 매매와 외국인 선물 순매수의 다일 예측력 (§7 Gap) | 미확인 | 찾지 못했다 | — |
| 32 | Park & Yi (2011), Fung et al. (2010) 원문 (§7 Gap) | 확인 + **정정** | Park & Yi: APJFS 40(3):347–376. Fung, Lam & Lam (2010): JEF 17(3):428–440. Fung et al.은 **아시아 지수선물 전반**을 다룬다. KOSPI200 선물이 표본에 들어가는지는 초록으로 확인하지 못했다. 상세는 §3 | [DOI Park-Yi](https://doi.org/10.1111/j.2041-6156.2011.01042.x), [IDEAS Fung](https://ideas.repec.org/a/eee/empfin/v17y2010i3p428-440.html) |
| 33 | 삼성전자·SK하이닉스와 SOX 연동 동료심사 논문 (§7 Gap) | 부분 | 지수 수준의 동료심사 논문: Byun, Kim & Park (2019). 종목 수준: Zhao (2026, 프리프린트). 상세는 §3 | [DOI](https://doi.org/10.29331/jkraic.2019.10.19.5.91), [arXiv](https://arxiv.org/abs/2608.03703) |
| 34 | KCMI 보고서 (§7 Gap) | 미확인 | 이번에 검색하지 않았다 | — |
| 35 | Jacobs & Müller (2020) 초록 (§8) | 확인 + **정정** | 39개 시장, 이상현상 241개, 200만 개 넘는 이상현상-국가-월 관측치. 발표 후 롱숏 수익이 신뢰할 만하게 줄어드는 나라는 **미국뿐**이다. 해석은 data mining이 아니라 mispricing과 시장 분절이다. JFE 135(1):213–230 | [IDEAS](https://ideas.repec.org/a/eee/jfinec/v135y2020i1p213-230.html) |
| 36 | Chordia, Subrahmanyam & Tong (2014) 수치 (§8) | 확인 | 대부분의 이상현상이 약해졌다. 주요 이상현상 포트폴리오 수익은 decimalization 뒤 **약 절반**이 됐다. 원인은 헤지펀드 AUM, 공매도, 회전율이다 | [IDEAS](https://ideas.repec.org/a/eee/jaecon/v58y2014i1p41-58.html) |
| 37 | 2024–2026 CTA/TSMOM 재검증 (§8 Gap) | 부분 | HOP에서 2010–2016년 성과가 크게 낮다(위 #5). 1개월 신호의 gross Sharpe는 0.06이다(§2). 2024–2026년 논문은 (미확인) | [AQR PDF](https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf) |
| 38 | 한국 이상현상의 발표 후 감쇠 (§8 Gap) | 부분 | Han, Lee & Kang (2020, JDQS): 한국에서 이상현상 148개를 재현했다. KOSPI+KOSDAQ 가치가중 기준으로 t>1.96은 37.8%, t>2.78은 27.7%뿐이다. microcap 영향이 크다. 발표 전후 비교는 아니다 | [DOI](https://doi.org/10.1108/jdqs-03-2020-0004) |

### Inferences
- 이전 노트에서 수치가 비어 있던 TSMOM, momentum, 감쇠 항목은 모두 메웠다. 결론의 방향(단일 자산 추세 신호는 약하다, 감쇠가 크다)은 바뀌지 않는다. 오히려 단기 신호의 감쇠가 더 크다는 근거가 추가됐다(#37).
- 가장 큰 변화는 #30과 #33이다. 대상 두 종목에서는 2026-05-27 전후로 미국 뉴스 전이와 다일 반전의 구조가 달라졌다는 종목 수준 근거가 나왔다. 다만 동료심사를 거치지 않은 프리프린트다.

### Gaps
- 여전히 미확인: #10, #11, #21, #22, #31, #34. 부분 확인 항목의 남은 세부도 있다: #1의 Sharpe, #15의 초록, #18의 결론 수치, #24의 표본 포함 여부.

---

## 2. 추세·momentum 핵심 수치와 감쇠 (MOP, HOP, Novy-Marx, Barroso–Santa-Clara, Jacobs–Müller)

### Takeaway
- 분산 선물 TSMOM의 gross Sharpe는 1을 넘는다(MOP, 1985–2009). 137년 백테스트에서 수수료와 비용을 빼면 Sharpe 0.76이다(HOP).
- 시계가 짧은 1개월 신호는 2010–2016년 gross Sharpe가 0.06으로 사실상 0이 됐다. 신호를 한 달 늦추면 1880–2016 전체에서도 1.38이 0.45로 떨어진다.
- 개별 자산 하나의 평균 Sharpe는 약 0.4다(HOP). 종목 셋에 쓰는 단일 자산 추세의 기대치는 낮다.

### Cited Findings
**MOP 2012** (JFE 104(2):228–250) — [원문 PDF](https://pages.stern.nyu.edu/~lpederse/papers/TimeSeriesMomentum.pdf), [IDEAS](https://ideas.repec.org/a/eee/jfinec/v104y2012i2p228-250.html)
- 구성:
  - 개별 상품: sign(지난 12개월 초과수익) × 40% ÷ 사전 변동성.
  - 팩터: 58종 동일가중이고, 연 변동성은 12%다.
- 분산 포트폴리오의 Sharpe는 연 1을 넘는다. 주식시장 포트폴리오의 약 2.5배이고 gross 기준이다.
- 표준 요인(시장지수, SMB, HML, UMD) 대비 alpha는 월 약 1.58%(분기 4.75%)다. "momentum everywhere" 요인을 통제해도 월 1.09%(t=5.40)다.
- 1966–1985년 표본 외 Sharpe는 1.1이다.
- 2008년 4분기에 크게 벌었고, 2009년 3–5월 위기 종료 때 급손실을 냈다.

**HOP 2017** (JPM 44(1):15–29) — [AQR PDF](https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf)
- 구성: 67개 시장(상품 29, 주가지수 11, 채권 15, 통화 12). 1, 3, 12개월 신호를 같은 비중으로 묶고, 목표 변동성은 연 10%다.
- 비용 가정: 자산군별 현재 비용 추정에 과거 비용이 더 높았던 것을 반영했다. 수수료는 2/20(high-water mark)이다.
- Exhibit 1, 1880–2016 연 초과수익:

  | 기준 | 값 |
  |---|---|
  | gross | 18.0% |
  | 비용 후 | 11.0% |
  | 수수료와 비용 후 | 7.3% |
  | 실현 변동성 | 9.7% |
  | 순 Sharpe | 0.76 |
  | 미국 주식과의 상관 | −0.01 |
  | 미국 10년 채권과의 상관 | −0.03 |

- 2010–2016년: gross 7.6%, 비용 후 6.2%, 순 3.3%, 순 Sharpe 0.41이다. 2000–2009년의 순 Sharpe는 0.61이다.
- Exhibit 2, 신호별 gross Sharpe(1880–2016):

  | 신호 | 그대로 | 1개월 지연 | 2010–2016 |
  |---|---|---|---|
  | 1개월 | 1.38 | 0.45 | **0.06** |
  | 3개월 | 1.19 | 0.64 | 0.30 |
  | 12개월 | 1.32 | 1.04 | 0.73 |

- 시장별 평균 Sharpe는 약 0.4다.
- 60/40 포트폴리오의 10대 낙폭 구간 중 8개에서 양(+)의 수익을 냈다.

**나머지 논문**
- **Novy-Marx 2012**: 최근 1–6개월 성과 기반 전략은 중기(12–7개월) 기반보다 약하다. 특히 최대형·고유동 종목에서 그렇다. — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v103y2012i3p429-453.html)
- **Barroso & Santa-Clara 2015**: momentum은 시장, value, size보다 Sharpe가 높지만 crash도 최악이다. 위험이 예측 가능해 관리하면 crash가 사실상 사라지고 Sharpe가 거의 2배가 된다. — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v116y2015i1p111-120.html)
- **Kim, Tse & Wald 2016**: 변동성 스케일링을 빼면 TSMOM ≈ buy-and-hold다. cross-sectional momentum의 alpha는 스케일링하지 않은 TSMOM보다 높다. — [IDEAS](https://ideas.repec.org/a/eee/finmar/v30y2016icp103-124.html)
- **J-T 2001**: momentum 이익이 1990년대에도 이어졌다. 원 결과가 data snooping이 아니라는 근거다. — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v56y2001i2p699-720.html)
- **Jacobs & Müller 2020**: 발표 후 감쇠가 확실한 나라는 미국뿐이다(39개 시장). — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v135y2020i1p213-230.html)
- **Chordia et al. 2014**: decimalization 뒤 이상현상 포트폴리오 수익이 약 절반이 됐다. — [IDEAS](https://ideas.repec.org/a/eee/jaecon/v58y2014i1p41-58.html)

### Inferences
- 스윙 시계에 가까운 1개월 추세 신호는 2010년대에 가장 크게 약해졌다(HOP Exhibit 2). 1개월 지연에도 민감하다. 종목 셋의 1–4주 추세 feature는 "국면 변수"로만 쓰라는 이전 결론을 강화한다.
- Novy-Marx의 "대형·고유동 종목에서 최근 수익 신호가 약하다"는 결과도 같은 방향이다. 삼성전자와 SK하이닉스가 바로 그런 종목이다.
- Jacobs & Müller에 따르면 한국 시장에서는 발표 후 감쇠가 미국보다 약할 수 있다. 그러나 Han et al.(2020)은 한국 이상현상의 상당수가 애초에 data mining이라고 본다. 둘을 합치면 "감쇠는 약하지만 처음부터 진짜인 신호가 적다"로 읽힌다.

### Gaps
- MOP의 비용 후 Sharpe는 원문에 없다. MOP는 gross만 보고하고, 증거금 사용률 5–20%만 언급한다.
- 2017년 이후 TSMOM 성과를 다룬 동료심사 논문은 이번에 찾지 않았다.

---

## 3. 미국→한국 전이: 장중 반전(Park & Yi, Fung et al.)과 미국 반도체→삼성전자·SK하이닉스 1–5일

### Takeaway
- 2011년 이전 표본에서는 한국 시장이 시가에 미국 야간 수익을 따르고, 장중에는 미국 수익과 반대로 움직였다(Park & Yi; Kim et al.; Fung et al.).
- 삼성전자와 SK하이닉스만 보면 2024.1–2026.5.26에는 미국 뉴스 반응이 다음날 반전되지 않았다. 종가 수익률의 1차 자기상관과 VR(5)도 0과 1에 가까웠다.
- 2026-05-27 단일종목 2배 LETF 16종 출시 뒤에는 첫날 반응의 약 75%가 다음날 종가까지 되돌려지고, 이후 며칠 진동했다(Zhao 2026, 프리프린트).

### Cited Findings
**미국 야간 정보와 한국 장중 반전 (2011년 이전 표본)**
- Park & Yi (2011, APJFS 40(3):347–376) "Mispricing of US Shocks in the Korean Stock Market" — [DOI](https://doi.org/10.1111/j.2041-6156.2011.01042.x)
  - 한국 시장은 시가에 미국 야간 수익을 가깝게 따른다. 그 뒤 장중 수익은 미국 수익과 음(-)의 관계다.
  - 분 단위 자료로 보면 반전은 장중에 점점 커진다.
  - 국내 개인은 미국 충격에 역행(contrarian)하고, 외국 기관은 순응(positive feedback)한다.
  - 외국인 매매는 미국 충격의 지속성에서 오는 향후 abnormal return을 예측한다.
- Fung, Lam & Lam (2010, JEF 17(3):428–440) "Do the prices of stock index futures in Asia overreact to US market returns?" — [IDEAS](https://ideas.repec.org/a/eee/empfin/v17y2010i3p428-440.html), [DOI](https://doi.org/10.1016/j.jempfin.2009.12.006)
  - 미국 시장이 극단적으로 움직인 뒤 아시아 지수선물에 장중 반전이 있다. 거래비용을 넣어도 수익 기회가 있다.
  - 위험, 유동성, 호가 스프레드로는 설명되지 않는다. 미국 변동 폭이 클수록 반전도 크다(magnitude effect).
  - 시장이 진정된 뒤에는 과잉반응이 크게 줄어든다. (오래된 결과)
  - 서지는 Park & Yi의 Crossref 참고문헌 목록에서 찾았다. — [Crossref](https://api.crossref.org/works/10.1111/j.2041-6156.2011.01042.x)
- Kim, Choi & Lee (2010, JITAM 17(3):151–162): KOSPI200 선물 시가→종가가 유의하게 반전한다. 그 반전을 이용한 장중 매매 시스템이 수익을 냈다고 보고한다. 표본 외 여부는 (미확인). — [earticle](https://www.earticle.net/Article/A166629)
- Lee & Hong (2019, preprints.org, 동료심사 아님): 한국 지수 ETF는 overnight 수익이 유의한 양(+)이고 장중 수익은 음(-)이다. — [DOI](https://doi.org/10.20944/preprints201905.0306.v1)
- Limkriangkrai, Chai & Zheng (2023, PBFJ): 첫 30분 수익률이 마지막 30분을 예측하는 시장 intraday momentum이 한국 ETF에서는 약하다. — [DOI](https://doi.org/10.1016/j.pacfin.2023.102086)

**미국 반도체 → 국내 반도체**
- Byun, Kim & Park (2019, Journal of Korea Research Association of International Commerce 19(5):91–109) — [DOI](https://doi.org/10.29331/jkraic.2019.10.19.5.91)
  - 필라델피아 반도체지수(SOX)의 상승과 하락이 KRX·KOSDAQ 반도체지수의 수익률과 변동성으로 전이된다.
  - SOX가 오르면(내리면) 국내 반도체 시장은 시가에서 과잉반응한 뒤 조정된다.
  - 외국인과 기관의 매매는 KRX 반도체지수의 야간 수익률, 주간 수익률과 모두 양(+)의 관계다.
  - 종목 수준이 아니라 지수 수준 연구다.

**Zhao (2026) "Preying on Leveraged ETFs"** (arXiv 2608.03703v4, 2026-08-29, 단독 저자, 동료심사 아님) — [arXiv](https://arxiv.org/abs/2608.03703), [PDF v4](https://arxiv.org/pdf/2608.03703v4)
- **배경**:
  - 2026-05-27에 삼성전자와 SK하이닉스를 기초로 한 단일종목 LETF 16종이 출시됐다. 6주 안에 16종 모두 상장가 아래로 떨어졌고, 보유자의 92%가 개인이다.
  - 출시 후 3개월 동안 KOSPI 서킷브레이커가 7회 발동했다.
  - KOSPI의 평균 일간 변동은 2025년 1.0%에서 4.1%가 됐다.
- **자료**: 2024.1–2026.7.31 종목-일 패널이다. 처치군은 두 종목, 대조군은 대형주 10, 반도체 동종 10, 미국 LETF 종목 8, 미국 지수 4다. 2026.5.4–7.31 KRX 1분 호가와 체결 자료도 썼다.
- **미국 shock 측정**: 한국 개장 전 SMH, QQQ, SPY 수익률을 도구변수로 썼다.
- **당일 반영 계수(QQQ 기준)**:
  - 두 종목 출시 후 1.59, 출시 전 0.77이다.
  - 한국 대조군은 0.59다.
- **다음날 종가-종가 반전 계수(SMH / QQQ / SPY)**:

  | 구간 | 계수 | 관측치 |
  |---|---|---|
  | 출시 후 두 종목 | **−0.83 / −1.76 / −3.92** (1% 유의) | 82 |
  | 출시 전 두 종목 | −0.07 / −0.09 / −0.07 (유의하지 않음) | 365 |
  | 대조군 | 출시 전후 모두 유의하지 않음 | — |

- **되돌림 비율**:
  - 삼중차분으로 보면 첫날 반응의 80%, 75%, 68%가 다음날 종가까지 되돌려진다.
  - 그중 32–45%는 다음날 시가 전(overnight)에 되돌려진다.
- **진동**: local projection에서 반전은 다음 이틀 동안 다시 반전한다. 3일째에는 계수가 첫날 수준 근처로 돌아온다.
- **반도체 사이클 요인 배제**: Nvidia와 AMD(미국 LETF가 있는 반도체 종목)에는 유의한 반전이 없다. 그래서 반도체 사이클이나 AI 뉴스로는 설명되지 않는다고 본다.
- **보정 모형 대비 자료값(Table 2)**:
  - 출시 후 종가 수익률 1차 자기상관(ρ1): SK하이닉스 −0.16, 삼성전자 −0.08.
  - 출시 전: ρ1 = 0.02 [−0.08, 0.12], VR(5) = 1.00.
  - 출시 후 하루 분산 중 overnight 비중은 0.62다.
- **반사실 추정**: SK하이닉스의 연 변동성은 LETF가 아주 작았다면 100.0%였을 것이다. 실현값은 136.7%다(9주 기준).
- 주의:
  - 출시 후 표본은 82 종목-일, 즉 약 41거래일이다.
  - 저자는 원고에 AI 도구를 썼다고 밝혔고, "preliminary, updated frequently" 버전이다.

**미국이 주는 방향(부호) 정보**
- Nyberg & Pönkä (2016, Economic Modelling 58:323–338): 미국과 10개 시장의 월간 초과수익 부호를 bivariate probit으로 함께 예측한다. 단변량 모형과 예측 회귀보다 표본 내외에서 낫고, market timing 성과도 낫다. 10개 시장에 한국이 들어가는지는 (미확인). — [IDEAS](https://ideas.repec.org/a/eee/ecmode/v58y2016icp323-338.html)

### Inferences
- "시가 갭은 미국을 따르고 장중에는 반전한다"는 이전 노트의 근거는 대부분 2003–2010년 표본이다. 최근 두 종목 자료(2024–2026.5)에서는 다음날 반전이 없고 ρ1≈0, VR(5)≈1이었다. 같은 날 시가→종가 반전이 최근에도 있는지는 Zhao의 출시 전 구간에서 따로 보고되지 않았다. 그러니 krx-quant 데이터로 직접 확인해야 한다.
- 2026-05-27 이후 두 종목의 일간 동학은 다른 체제다. 미국 shock 반응이 커지고, 다음날 반전과 2–3일 진동이 생겼다. 1–5일 예측에서 "미국 shock × 출시 후 여부" 상호작용이 문헌상 가장 직접적인 후보다. 다만 41거래일짜리 프리프린트 결과다.
- Byun et al.(2019)과 Park & Yi(2011)를 같이 보면 외국인 매매는 미국 정보의 지속분과 같은 방향이다. 개인은 반대다. 투자자별 수급을 미국 shock과 상호작용시키는 feature가 근거와 맞는다.

### Gaps
- 삼성전자와 SK하이닉스 종목 수준의 SOX/NVDA/MU 1–5일 전이를 다룬 동료심사 논문은 여전히 없다. Micron 실적 발표가 SK하이닉스에 주는 read-through를 다룬 연구도 찾지 못했다.
- Fung, Lam & Lam(2010)의 표본 국가 목록, 출시 전 두 종목의 같은 날 시가→종가 반전 여부(Zhao 부록)는 확인하지 못했다.

---

## 4. 한국 스윙 근거 보강: 다일 momentum/reversal, 외국인·기관 수급 지속성, KOSPI200 리밸런싱과 만기

### Takeaway
- 한국 대형주의 일간 수익률은 최근(2024–2026.5) 자기상관이 거의 없다(Zhao). 지수 수준에서는 2000년대 이후 세계적으로 음(-)의 자기상관이 일반화됐다(Baltussen et al.).
- 외국인은 positive feedback 매매가 일관되게 확인된다. 삼성전자에서도 같다. 그러나 순매수의 여러 날 지속이 며칠 뒤 수익을 예측한다는 동료심사 근거는 찾지 못했다.
- 2015년 이후 KOSPI200 변경 효과와 만기 효과를 다룬 동료심사 논문은 없다. 만기일 종가 조작 시도의 가격 영향이 유의하지 않다는 연구(Yang 2018)와 언론 분석만 있다.

### Cited Findings
**다일 momentum과 reversal**
- Zhao (2026): 두 종목의 출시 전 ρ1 = 0.02 [−0.08, 0.12], VR(5) = 1.00. 출시 후 ρ1은 SK하이닉스 −0.16, 삼성전자 −0.08이다(프리프린트). — [arXiv](https://arxiv.org/abs/2608.03703)
- Baltussen, van Bekkum & Da (2019, JFE 132(1):26–48) — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v132y2019i1p26-48.html)
  - 15개국의 주요 지수 20개에서 지수 수익률의 자기상관이 1990년대까지는 양(+)이었다가 2000년대 이후 음(-)이 됐다.
  - 이 변화는 지수 상품(선물, ETF, 인덱스펀드)의 확산과 연결된다.
  - KOSPI가 표본에 들어가는지는 (미확인).
- Kongahawatte (2025, Asian Finance Review, 신생 학술지): 한국 2014–2017년에 overnight momentum과 intraday momentum이 견고하고, 두 구간을 가로지르는 반전이 이를 상쇄한다. 개인의 주목 매매(overnight)와 국내 기관의 차익거래(intraday)로 설명한다. — [DOI](https://doi.org/10.31357/afr.v2i02.7881)
- Lou, Polk & Skouras (2019, JFE 134(1):192–213), 미국: 기업 수준에서 overnight 수익과 intraday 수익은 각각 지속되고, 서로 상쇄하는 교차 반전이 있다. reversal과 여러 momentum 전략의 이익은 전부 overnight에서 난다. — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v134y2019i1p192-213.html)
- 이명철·이수건 (2011): 외환위기 뒤에는 52주 고가 momentum이 우세하다(§1 #23). — [earticle](https://www.earticle.net/Article/A164768)
- Sim (2026, EMFT): 액티브 펀드 이탈이 단기 반전을 키운다(§1 #2). — [DOI](https://doi.org/10.1080/1540496x.2026.2617473)
- Butt, Högholm & Sadaqat (2020, JMFM): 여러 신흥시장에서 단기 반전 alpha가 유의하다. 시장 변동성이 높을 때, 액티브 투자자의 이탈이 클 때 더 크다. 한국 포함 여부는 (미확인). — [DOI](https://doi.org/10.1016/j.mulfin.2020.100664)

**외국인·기관 수급**
- Kim & Jo (2019, Sustainability): 2007.1–2008.12 KOSPI 상위 9개 기업이 대상이다. 외국인은 **삼성전자** 주가에 대해 강한 positive feedback 매매를 했다. 외국인 매매는 삼성전자와 현대차의 변동성을 유의하게 키웠다. (오래된 결과) — [DOI](https://doi.org/10.3390/su11061576)
- Yang, Samitas & Kampouris (2021): 외국인은 수익률이 오른 다음날 더 많이 산다. 국내 기관의 순매수는 다음날 수익률을 양(+)으로, 개인의 순매수는 음(-)으로 예측한다. — [DOI](https://doi.org/10.1108/IJMF-01-2020-0022)
- Richards (2005): 외국인은 글로벌 수익률에도 feedback 매매를 하고, 가격 영향이 크다. — [IDEAS](https://ideas.repec.org/a/cup/jfinqa/v40y2005i01p1-27_00.html)
- Ülkü & Weber (2013, Review of Finance): 동시점의 상호작용을 식별하는 SCC 방법을 쓰면 일부 아시아 시장에서 "외국인 positive feedback"이라는 기존의 획일적 결론이 바뀐다. 외국인은 글로벌 정보에 느리게 반응한다. — [DOI](https://doi.org/10.1093/rof/rft029)
- Lee & Kim (2022, PBFJ): 한국에서는 외국인이 월말·월초(TOM)에 더 많이 사고, 그 종목들이 초과성과를 낸다(§5). — [IDEAS](https://ideas.repec.org/a/eee/pacfin/v75y2022ics0927538x22001214.html)
- 이화여대 연구(서지 미확인): 외국인 순매수비율이 직전 순매수비율과 양(+)의 관계, 즉 지속성이 있다는 내용이다. 검색 요약으로만 봤고 페이지는 403으로 막혔다. (미확인) — [Ewha dspace](https://dspace.ewha.ac.kr/handle/2015.oak/179183)

**KOSPI200 리밸런싱과 만기**
- Yang (2018, 재무연구 47(5):709–739) "An Empirical Analysis of Stock Price Manipulation in the Expiration Days of Futures and Options" — [KJFS](https://doi.org/10.26845/kjfs.2018.10.47.5.709)
  - 현물과 선물 계좌를 동시에 가진 투자자 자료를 썼다.
  - 선물 롱(숏) 포지션이 클수록 만기일에 더 높은(낮은) 가격의 지정가 매수(매도) 주문을 낸다. Kumar-Seppi 예측과 맞는다.
  - 그러나 그 주문의 가격 영향은 통계적으로 유의하지 않다. 표본 기간은 초록에 없다.
- 2019-07 거래소가 매주 목요일 만기 위클리 옵션 상장 계획을 발표했다(실제 도입일은 미확인). 만기 효과를 분석할 때는 이 시점 전후로 구간을 나눠야 한다. — [아주경제 2019-07-31](https://www.ajunews.com/view/20190731181038818)
- 2017–2019년 KOSPI200 신규 편입 17종목 중 13종목이 변경일 직후 하락했다(언론, 비학술). — [뉴스토마토](https://newstomato.com/ReadNews.aspx?no=977218)
- Yun & Kim (2010): 영구 효과와 부분 반전, 효력일 전 선행매매. (오래된 결과) — [IDEAS](https://ideas.repec.org/a/eee/finana/v19y2010i4p258-269.html)

**한국 이상현상 전반**
- Han, Lee & Kang (2020): 148개 중 t>1.96은 37.8%다. data mining 비중이 크다고 결론짓는다. — [DOI](https://doi.org/10.1108/jdqs-03-2020-0004)

### Inferences
- 두 종목 일간 수익률의 무조건부 자기상관은 출시 전에는 거의 0이다. "직전 1–5일 수익만으로 방향 맞히기"는 출시 전 구간에서 hit rate가 기준선 근처일 가능성이 높다. 신호가 있다면 수급, 미국 shock, 변동성 같은 조건부 정보에서 나와야 한다.
- 외국인 feedback 매매가 일관되게 확인되므로, 수급 자체보다 "가격 변화 뒤 외국인의 반응"이 정보일 수 있다. 다만 며칠 뒤 수익 예측력은 근거가 없으니 검증 대상이다.
- 세 종목은 KOSPI200 편입·제외 이벤트가 없다. 지수 리밸런싱일과 만기일은 "그날 종가 노이즈" flag로만 둔다는 이전 결론을 유지한다. Yang(2018)도 만기 조작의 가격 영향이 유의하지 않다고 본다.

### Gaps
- 외국인·기관 순매수의 5–20일 누적이 1–5일 뒤 수익을 예측하는지 직접 검정한 동료심사 논문은 찾지 못했다.
- 2015년 이후 KOSPI200 정기변경과 옵션 만기의 수익률 효과를 다룬 동료심사 논문은 찾지 못했다. Yang(2018)의 표본 기간도 미확인이다.
- MSCI Korea 리밸런싱이 대형주 수급에 미치는 영향을 다룬 연구는 찾지 못했다.

---

## 5. 이전 노트에 빠진 스윙 시계 기법 (한 줄씩)

### Takeaway
근거가 탄탄하고 krx-quant 데이터로 바로 flag를 만들 수 있는 것은 셋이다. turn-of-the-month(한국 근거 있음), pre-FOMC drift(국제 지수 근거 있음), 실적 발표 프리미엄이다. 공매도 신호는 한국에서 2023-11부터 2025-03까지 전면 금지 기간이 있어 일관된 시계열이 아니다.

### Cited Findings
- **Turn-of-the-month**: McConnell & Xu (2008, FAJ). 미국 1926–2005년에 주식 위험 보상이 사실상 월말·월초에만 생겼다. 35개국 중 31개국에서 확인됐고, 월말 매수 압력으로는 설명되지 않는다. — [DOI](https://doi.org/10.2469/faj.v64.n2.11)
  - 한국: Lee & Kim (2022, PBFJ). TOM 효과가 유의하다. 규모, 연말, 분기말, 지수 리밸런싱으로 설명되지 않는다. 외국인이 일부 기여한다. — [IDEAS](https://ideas.repec.org/a/eee/pacfin/v75y2022ics0927538x22001214.html)
- **Pre-holiday**: Ariel (1990, JF). 미국 1963–1982년에 휴장 전날 평균 수익이 다른 날의 9–14배였다. 연 8일이 시장 총수익의 3분의 1을 넘었다. (오래된 결과) — [DOI](https://doi.org/10.1111/j.1540-6261.1990.tb03731.x)
  - 한국: Segaran & Chia (2021, 소규모 학술지)는 KOSPI에서 유의하지 않다고 보고한다. — [DOI](https://doi.org/10.51200/lbibf.v19i1.2930)
- **Pre-FOMC drift**: Lucca & Moench (2015, JF). 정례 FOMC 직전에 미국 주식 초과수익이 크다. 다른 주요 국제 지수에도 비슷한 효과가 있다. — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v70y2015i1p329-371.html)
- **실적 발표 프리미엄**:
  - Frazzini & Lamont (2007, NBER w13090): 정례 실적 발표일 전후로 주가가 오른다. 발표 주목에 따른 소액 투자자의 매수와 관련된다. — [DOI](https://doi.org/10.3386/w13090)
  - Barber, De George, Lehavy & Trueman (2013, JFE): 20개국 중 9개국에서 유의하다. 발표 전후 고유 변동성이 크게 오르는 나라에서 강하다. 한국이 유의한 9개국에 들어가는지는 (미확인). — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v108y2013i1p118-138.html)
- **공매도(short interest)**:
  - Boehmer, Jones & Zhang (2008, JF): NYSE 2000–2004년. 공매도가 많은 종목은 다음 20거래일 동안 위험조정 1.16%(연 15.6%) 저조하다. — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v63y2008i2p491-527.html)
  - Rapach, Ringgenberg & Zhou (2016, JFE): 총 공매도 잔고가 시장 수익률의 가장 강한 예측변수다. 연 R²는 표본 내 12.89%, 표본 외 13.24%다. — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v121y2016i1p46-65.html)
  - 한국: Yoo (2016)는 2005.1–2016.3 대차 관련 변수(차분)와 고유수익률이 음(-)의 관계라고 보고한다. 동시점 분석에 가깝다. — [DOI](https://doi.org/10.5762/kais.2016.17.6.560)
  - 공매도 전면 금지 기간: 2023-11부터 2025-03-30까지다. 2025-03-31에 전 종목 재개됐다. — [한국경제](https://www.hankyung.com/article/2025032106066)
- **애널리스트 수정 drift**:
  - Gleason & Lee (2003, TAR): 수정 뒤 drift가 있다. 혁신성이 큰 수정, 덜 알려진 애널리스트, 커버리지가 적은 기업에서 크다. — [DOI](https://doi.org/10.2308/accr.2003.78.1.193)
  - Su (2023, Financial Review, 글로벌): 1–6개월 보유 수정 전략은 시변 위험프리미엄을 통제하면 수익이 사라지거나 크게 준다. — [DOI](https://doi.org/10.1111/fire.12357)
- **공급망 momentum**: Cohen & Frazzini (2008). 고객사 뉴스가 공급사 주가에 늦게 반영된다. 월 alpha가 150bp를 넘는다. — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v63y2008i4p1977-2011.html)
- **지수 자기상관의 음(-)전환**: Baltussen et al. (2019)(§4). — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v132y2019i1p26-48.html)

### Inferences
- TOM과 FOMC 일정은 결정론적 달력 flag라 data-snooping 비용이 거의 없다. 5거래일 예측의 보조 입력으로 적합하다.
- NVDA와 Micron 실적은 공급망 momentum(Cohen-Frazzini)과 실적 발표 프리미엄의 교집합이다. SK하이닉스와 삼성전자의 "업계 실적 이벤트 flag" 근거가 된다. 한국 종목 수준 검증은 없다.

### Gaps
- 한국 휴장 전날, 실적 발표 프리미엄, 애널리스트 수정 drift의 최근 대형주 연구는 찾지 못했다.

---

## krx-quant 적용 메모 (1차 권고 중 바뀌는 것만)

1. **2026-05-27 체제 구분이 필요하다.**
   - 1분봉 1년치(약 2025-10 → 2026-10으로 추정)는 단일종목 LETF 출시 전후에 걸쳐 있다. #264(하루 한 번 예측)의 학습과 검증은 출시 전/후로 나눠 보고해야 한다.
   - 출시 후 표본은 2026-10 현재 약 100거래일 안팎(추정)으로 작다.
   - flat 임계값을 "변동성 × √h × k"로 정한다는 1차 권고는 유지한다. 단, 변동성은 반드시 rolling으로 추정해야 한다. KOSPI 평균 일간 변동이 1.0%에서 4.1%로 바뀌었다(Zhao 2026).
2. **"D+1 시가→종가 = 갭 반전" 가정은 부호를 미리 정하지 않는다.**
   - 장중 반전 근거는 2003–2010년 표본이다(Park & Yi, Kim et al., Fung et al.).
   - 최근 두 종목에 대한 문헌 근거는 반대로, 출시 전 다음날 반전 없음, 출시 후 다음날 반전과 2–3일 진동이다.
   - 1차 권고의 1번(D+1 갭)과 2번(D+1 시가→종가)은 유지한다. 대신 입력에 "전일 미국 shock(SMH/QQQ, 이미 보유한 SOX/NVDA/MU로 대체 가능) × 출시 후 dummy"와 "전전일 미국 shock"을 넣어 진동을 잡게 한다.
3. **5거래일 방향에서 자기 수익률 기반 feature의 기대를 낮춘다.**
   - 출시 전 ρ1≈0, VR(5)=1.00이다(Zhao).
   - HOP의 1개월 추세 신호는 2010–2016년 Sharpe 0.06이다. Novy-Marx도 대형주에서 최근 수익 신호가 약하다고 본다.
   - 추세와 52주 고가는 국면 변수로만 쓴다는 1차 결론을 강화한다.
4. **달력 flag에 TOM을 추가한다.** 한국에서 근거가 있고(Lee & Kim 2022), 외국인 매수와 연결된다. 만기일과 지수 리밸런싱일은 노이즈 flag로 유지한다(Yang 2018).
5. **10년 수급과 공매도 자료는 2023-11~2025-03 공매도 금지 구간을 따로 표시해야 한다.** 이 기간의 외국인·기관 매도는 성격이 다르다.
